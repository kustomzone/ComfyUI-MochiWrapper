import os
import torch
import folder_paths
import comfy.model_management as mm
from comfy.utils import ProgressBar, load_torch_file
from einops import rearrange
from tqdm import tqdm
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

from .mochi_preview.t2v_synth_mochi import T2VSynthMochiModel
from .mochi_preview.vae.model import Decoder

script_directory = os.path.dirname(os.path.abspath(__file__))

def linear_quadratic_schedule(num_steps, threshold_noise, linear_steps=None):
    if linear_steps is None:
        linear_steps = num_steps // 2
    linear_sigma_schedule = [i * threshold_noise / linear_steps for i in range(linear_steps)]
    threshold_noise_step_diff = linear_steps - threshold_noise * num_steps
    quadratic_steps = num_steps - linear_steps
    quadratic_coef = threshold_noise_step_diff / (linear_steps * quadratic_steps ** 2)
    linear_coef = threshold_noise / linear_steps - 2 * threshold_noise_step_diff / (quadratic_steps ** 2)
    const = quadratic_coef * (linear_steps ** 2)
    quadratic_sigma_schedule = [
        quadratic_coef * (i ** 2) + linear_coef * i + const
        for i in range(linear_steps, num_steps)
    ]
    sigma_schedule = linear_sigma_schedule + quadratic_sigma_schedule + [1.0]
    sigma_schedule = [1.0 - x for x in sigma_schedule]
    return sigma_schedule
   
class DownloadAndLoadMochiModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (
                    [   
                        "mochi_preview_dit_fp8_e4m3fn.safetensors",
                    ],
                ),
                "vae": (
                    [   
                        "mochi_preview_vae_bf16.safetensors",
                    ],
                ),
                 "precision": (["fp8_e4m3fn","fp16", "fp32", "bf16"],
                    {"default": "fp8_e4m3fn", }
                ),
            },
        }

    RETURN_TYPES = ("MOCHIMODEL", "MOCHIVAE",)
    RETURN_NAMES = ("mochi_model", "mochi_vae" )
    FUNCTION = "loadmodel"
    CATEGORY = "MochiWrapper"
    DESCRIPTION = "Downloads and loads the selected Mochi model from Huggingface"

    def loadmodel(self, model, vae, precision):

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        mm.soft_empty_cache()

        dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        # Transformer model
        model_download_path = os.path.join(folder_paths.models_dir, 'diffusion_models', 'mochi')
        model_path = os.path.join(model_download_path, model)
   
        repo_id = "kijai/Mochi_preview_comfy"
        
        if not os.path.exists(model_path):
            log.info(f"Downloading mochi model to: {model_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=repo_id,
                allow_patterns=[f"*{model}*"],
                local_dir=model_download_path,
                local_dir_use_symlinks=False,
            )
        # VAE
        vae_download_path = os.path.join(folder_paths.models_dir, 'vae', 'mochi')
        vae_path = os.path.join(vae_download_path, vae)

        if not os.path.exists(vae_path):
            log.info(f"Downloading mochi VAE to: {vae_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=repo_id,
                allow_patterns=[f"*{vae}*"],
                local_dir=model_download_path,
                local_dir_use_symlinks=False,
            )

        model = T2VSynthMochiModel(
            device_id=0,
            vae_stats_path=os.path.join(script_directory, "configs", "vae_stats.json"),
            dit_checkpoint_path=model_path,
            weight_dtype=dtype,
        )
        vae = Decoder(
                out_channels=3,
                base_channels=128,
                channel_multipliers=[1, 2, 4, 6],
                temporal_expansions=[1, 2, 3],
                spatial_expansions=[2, 2, 2],
                num_res_blocks=[3, 3, 4, 6, 3],
                latent_dim=12,
                has_attention=[False, False, False, False, False],
                padding_mode="replicate",
                output_norm=False,
                nonlinearity="silu",
                output_nonlinearity="silu",
                causal=True,
            )
        decoder_sd = load_torch_file(vae_path)
        vae.load_state_dict(decoder_sd, strict=True)
        vae.eval().to(torch.bfloat16).to("cpu")
        del decoder_sd

        return (model, vae,)
    
class MochiTextEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "clip": ("CLIP",),
            "prompt": ("STRING", {"default": "", "multiline": True} ),
            },
            "optional": {
                "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "force_offload": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("conditioning",)
    FUNCTION = "process"
    CATEGORY = "MochiWrapper"

    def process(self, clip, prompt, strength=1.0, force_offload=True):
        max_tokens = 256
        load_device = mm.text_encoder_device()
        offload_device = mm.text_encoder_offload_device()
        #print(clip.tokenizer.t5xxl)
        clip.tokenizer.t5xxl.pad_to_max_length = True
        clip.tokenizer.t5xxl.max_length = max_tokens
        clip.cond_stage_model.t5xxl.return_attention_masks = True
        clip.cond_stage_model.t5xxl.enable_attention_masks = True
        clip.cond_stage_model.t5_attention_mask = True
        clip.cond_stage_model.to(load_device)
        tokens = clip.tokenizer.t5xxl.tokenize_with_weights(prompt, return_word_ids=True)
        
        embeds, _, attention_mask = clip.cond_stage_model.t5xxl.encode_token_weights(tokens)


        if embeds.shape[1] > 256:
            raise ValueError(f"Prompt is too long, max tokens supported is {max_tokens} or less, got {embeds.shape[1]}")
        embeds *= strength
        if force_offload:
            clip.cond_stage_model.to(offload_device)

        t5_embeds = {
            "embeds": embeds,
            "attention_mask": attention_mask["attention_mask"].bool(),
        }
        return (t5_embeds, )
    

class MochiSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MOCHIMODEL",),
                "positive": ("CONDITIONING", ),
                "negative": ("CONDITIONING", ),
                "width": ("INT", {"default": 848, "min": 128, "max": 2048, "step": 8}),
                "height": ("INT", {"default": 480, "min": 128, "max": 2048, "step": 8}),
                "num_frames": ("INT", {"default": 49, "min": 7, "max": 1024, "step": 6}),
                "steps": ("INT", {"default": 50, "min": 2}),
                "cfg": ("FLOAT", {"default": 4.5, "min": 0.0, "max": 30.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("model", "samples",)
    FUNCTION = "process"
    CATEGORY = "MochiWrapper"

    def process(self, model, positive, negative, steps, cfg, seed, height, width, num_frames):
        mm.soft_empty_cache()

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        args = {
            "height": height,
            "width": width,
            "num_frames": num_frames,
            "mochi_args": {
                "sigma_schedule": linear_quadratic_schedule(steps, 0.025),
                "cfg_schedule": [cfg] * steps,
                "num_inference_steps": steps,
                "batch_cfg": False,
            },
            "positive_embeds": positive,
            "negative_embeds": negative,
            "seed": seed,
        }
        latents = model.run(args, stream_results=False)
    
        mm.soft_empty_cache()

        return ({"samples": latents},)
    
class MochiDecode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "vae": ("MOCHIVAE",),
            "samples": ("LATENT", ),
            "enable_vae_tiling": ("BOOLEAN", {"default": False, "tooltip": "Drastically reduces memory use but may introduce seams"}),
            },
            "optional": {
            "tile_sample_min_height": ("INT", {"default": 240, "min": 16, "max": 2048, "step": 8, "tooltip": "Minimum tile height, default is half the height"}),
            "tile_sample_min_width": ("INT", {"default": 424, "min": 16, "max": 2048, "step": 8, "tooltip": "Minimum tile width, default is half the width"}),
            "tile_overlap_factor_height": ("FLOAT", {"default": 0.1666, "min": 0.0, "max": 1.0, "step": 0.001}),
            "tile_overlap_factor_width": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0, "step": 0.001}),
            "auto_tile_size": ("BOOLEAN", {"default": True, "tooltip": "Auto size based on height and width, default is half the size"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "decode"
    CATEGORY = "MochiWrapper"

    def decode(self, vae, samples, enable_vae_tiling, tile_sample_min_height, tile_sample_min_width, tile_overlap_factor_height, tile_overlap_factor_width, auto_tile_size=True):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        samples = samples["samples"]

        def blend_v(a: torch.Tensor, b: torch.Tensor, blend_extent: int) -> torch.Tensor:
            blend_extent = min(a.shape[3], b.shape[3], blend_extent)
            for y in range(blend_extent):
                b[:, :, :, y, :] = a[:, :, :, -blend_extent + y, :] * (1 - y / blend_extent) + b[:, :, :, y, :] * (
                    y / blend_extent
                )
            return b

        def blend_h(a: torch.Tensor, b: torch.Tensor, blend_extent: int) -> torch.Tensor:
            blend_extent = min(a.shape[4], b.shape[4], blend_extent)
            for x in range(blend_extent):
                b[:, :, :, :, x] = a[:, :, :, :, -blend_extent + x] * (1 - x / blend_extent) + b[:, :, :, :, x] * (
                    x / blend_extent
                )
            return b

        self.tile_overlap_factor_height = tile_overlap_factor_height if not auto_tile_size else 1 / 6
        self.tile_overlap_factor_width = tile_overlap_factor_width if not auto_tile_size else 1 / 5
        #7, 13, 19, 25, 31, 37, 43, 49, 55, 61, 67, 73, 79, 85, 91, 97, 103, 109, 115, 121, 127, 133, 139, 145, 151, 157, 163, 169, 175, 181, 187, 193, 199
        self.num_latent_frames_batch_size = 6 

        self.tile_sample_min_height = tile_sample_min_height if not auto_tile_size else samples.shape[3] // 2 * 8
        self.tile_sample_min_width = tile_sample_min_width if not auto_tile_size else samples.shape[4] // 2 * 8

        self.tile_latent_min_height = int(self.tile_sample_min_height / 8)
        self.tile_latent_min_width = int(self.tile_sample_min_width / 8)
        
        vae.to(device)
        with torch.amp.autocast("cuda", dtype=torch.bfloat16):
            if not enable_vae_tiling:
                samples = vae(samples)
            else:
                batch_size, num_channels, num_frames, height, width = samples.shape
                overlap_height = int(self.tile_latent_min_height * (1 - self.tile_overlap_factor_height))
                overlap_width = int(self.tile_latent_min_width * (1 - self.tile_overlap_factor_width))
                blend_extent_height = int(self.tile_sample_min_height * self.tile_overlap_factor_height)
                blend_extent_width = int(self.tile_sample_min_width * self.tile_overlap_factor_width)
                row_limit_height = self.tile_sample_min_height - blend_extent_height
                row_limit_width = self.tile_sample_min_width - blend_extent_width
                frame_batch_size = self.num_latent_frames_batch_size

                # Split z into overlapping tiles and decode them separately.
                # The tiles have an overlap to avoid seams between tiles.
                rows = []
                for i in tqdm(range(0, height, overlap_height), desc="Processing rows"):
                    row = []
                    for j in tqdm(range(0, width, overlap_width), desc="Processing columns", leave=False):
                        time = []
                        for k in tqdm(range(num_frames // frame_batch_size), desc="Processing frames", leave=False):
                            remaining_frames = num_frames % frame_batch_size
                            start_frame = frame_batch_size * k + (0 if k == 0 else remaining_frames)
                            end_frame = frame_batch_size * (k + 1) + remaining_frames
                            tile = samples[
                                :,
                                :,
                                start_frame:end_frame,
                                i : i + self.tile_latent_min_height,
                                j : j + self.tile_latent_min_width,
                            ]
                            tile = vae(tile)
                            time.append(tile)
                        row.append(torch.cat(time, dim=2))
                    rows.append(row)

                result_rows = []
                for i, row in enumerate(tqdm(rows, desc="Blending rows")):
                    result_row = []
                    for j, tile in enumerate(tqdm(row, desc="Blending tiles", leave=False)):
                        # blend the above tile and the left tile
                        # to the current tile and add the current tile to the result row
                        if i > 0:
                            tile = blend_v(rows[i - 1][j], tile, blend_extent_height)
                        if j > 0:
                            tile = blend_h(row[j - 1], tile, blend_extent_width)
                        result_row.append(tile[:, :, :, :row_limit_height, :row_limit_width])
                    result_rows.append(torch.cat(result_row, dim=4))

                samples = torch.cat(result_rows, dim=3)
        vae.to(offload_device)
        #print("samples", samples.shape, samples.dtype, samples.device)

        samples = samples.float()
        samples = (samples + 1.0) / 2.0
        samples.clamp_(0.0, 1.0)

        frames = rearrange(samples, "b c t h w -> (t b) h w c").cpu().float()
        #print(frames.shape)

        return (frames,)


NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadMochiModel": DownloadAndLoadMochiModel,
    "MochiSampler": MochiSampler,
    "MochiDecode": MochiDecode,
    "MochiTextEncode": MochiTextEncode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadMochiModel": "(Down)load Mochi Model",
    "MochiSampler": "Mochi Sampler",
    "MochiDecode": "Mochi Decode",
    "MochiTextEncode": "Mochi TextEncode",
    }
