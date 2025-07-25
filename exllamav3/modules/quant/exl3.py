from __future__ import annotations
import torch
from ...models.config import Config
from ...util.tensor import to2
from ...util import first_not_none
import math
from .exl3_lib.quantize import preapply_had_l, preapply_had_r, had_k, had_n, tensor_core_perm, tensor_core_perm_i
from ...ext import exllamav3_ext as ext
from ...util import profile_opt

class LinearEXL3:

    def __init__(
        self,
        config: Config | None,
        in_features: int,
        out_features: int,
        scale: torch.Tensor | None = None,
        su: torch.Tensor | None = None,
        sv: torch.Tensor | None = None,
        suh: torch.Tensor | None = None,
        svh: torch.Tensor | None = None,
        trellis: torch.Tensor | None = None,
        mcg: torch.Tensor | None = None,
        mul1: torch.Tensor | None = None,
        bias: torch.Tensor | None = None,
        out_dtype: torch.dtype | None = None,
        transformers_fix: bool = False
    ):
        assert scale is None, "scale is no longer used"
        assert su is not None or suh is not None, "either su (packed) or suh (unpacked) is required"
        assert sv is not None or svh is not None, "either sv (packed) or svh (unpacked) is required"
        assert trellis is not None, "trellis is required"
        if su is not None: assert su.dtype == torch.int16, "su is wrong datatype"
        if sv is not None: assert sv.dtype == torch.int16, "sv is wrong datatype"
        if suh is not None: assert suh.dtype == torch.half, "suh is wrong datatype"
        if svh is not None: assert svh.dtype == torch.half, "svh is wrong datatype"
        assert trellis.dtype == torch.int16, "trellis is wrong datatype"
        assert len(trellis.shape) == 3, "trellis must have dim = 3"

        if bias is not None and bias.dtype == torch.float: bias = bias.to(torch.half)

        self.transformers_fix = transformers_fix

        # self.scale = scale.item()
        self.su = None
        self.sv = None
        self.suh = suh if suh is not None else self.unpack_bf(su)
        self.svh = svh if svh is not None else self.unpack_bf(sv)
        self.trellis = trellis
        self.K = trellis.shape[-1] // 16
        self.in_features = in_features
        self.out_features = out_features
        self.bias = bias
        self.swap_device = None
        self.out_dtype = out_dtype
        self.mcg_mult = None

        self.mcg = mcg
        self.mcg_mult = mcg.view(torch.uint32).item() if mcg is not None else 0

        self.mul1 = mul1
        self.mul1_mult = mul1.view(torch.uint32).item() if mul1 is not None else 0


    def get_tensors(self, key: str):
        return {
            f"{key}.{subkey}": tensor.contiguous()
            for subkey, tensor in [
                ("su", self.su),
                ("sv", self.sv),
                ("suh", self.suh),
                ("svh", self.svh),
                ("trellis", self.trellis),
                ("bias", self.bias),
                ("mcg", self.mcg),
                ("mul1", self.mul1),
            ] if tensor is not None
        }


    def forward(
        self,
        x: torch.Tensor,
        params: dict,
        out_dtype: torch.dtype | None = None,
    ) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:

        out_shape = x.shape[:-1] + (self.out_features,)
        x = x.view(-1, self.in_features)

        torch_mode = params.get("reconstruct", x.shape[0] > 32)
        ret_dtype = out_dtype or self.out_dtype or torch.half

        if torch_mode:
            y = torch.empty(out_shape, dtype = ret_dtype, device = x.device)
            y_ = y.view(x.shape[0], self.out_features)
            xh = torch.empty_like(x)
            ext.had_r_128(x, xh, self.suh, None, 1.0)
            w = self.get_inner_weight_tensor()
            ext.hgemm(xh, w, y_)
            ext.had_r_128(y_, y_, None, self.svh, 1.0)
        else:
            y = torch.empty(out_shape, dtype = ret_dtype, device = x.device)
            xh = torch.empty_like(x)
            ext.exl3_gemm(x, self.trellis, y, self.suh, xh, self.svh, -1, self.mcg_mult, self.mul1_mult)

        if self.bias is not None:
            y += self.bias

        return y


    def unpack_bf(self, bitfield: torch.Tensor):
        # For some reason this operation causes a GPU assert on Transformers. Running on CPU seems to fix it
        device = bitfield.device
        if self.transformers_fix:
            bitfield = bitfield.cpu()

        # TODO: Maybe custom kernel for this. Only used for full reconstruct and loading old models, not during inference
        bitfield = bitfield.view(torch.uint16).to(torch.int)
        masks = (1 << torch.arange(16)).to(bitfield.device)
        expanded = (bitfield.unsqueeze(-1) & masks) > 0
        expanded = expanded.flatten()
        expanded = torch.where(expanded, torch.tensor(-1.0, dtype = torch.float16), torch.tensor(1.0, dtype = torch.float16))
        return expanded.contiguous().to(device)


    def get_weight_tensor(self):
        # suh = self.unpack_bf(self.su).unsqueeze(1)
        suh = self.unpack_bf(self.su).unsqueeze(1) if self.su else self.suh.unsqueeze(1)
        svh = self.unpack_bf(self.sv).unsqueeze(0) if self.sv else self.svh.unsqueeze(0)
        w = self.get_inner_weight_tensor()
        w = preapply_had_l(w, had_k)
        w *= suh
        w = preapply_had_r(w, had_n)
        w *= svh
        # w *= self.scale
        return w


    def get_inner_weight_tensor(self):
        w = torch.empty((self.in_features, self.out_features), dtype = torch.half, device = self.trellis.device)
        ext.reconstruct(w, self.trellis, self.K, self.mcg_mult, self.mul1_mult)
        return w


    def get_bias_tensor(self) -> torch.Tensor | None:
        return self.bias


    # Swap tensors to CPU (to free some space while quantizing)
    def swap_cpu(self):
        if self.swap_device is not None:
            return
        self.swap_device = self.trellis.device
        if self.su is not None: self.su = self.su.cpu()
        if self.sv is not None: self.sv = self.sv.cpu()
        if self.suh is not None: self.suh = self.suh.cpu()
        if self.svh is not None: self.svh = self.svh.cpu()
        if self.trellis is not None: self.trellis = self.trellis.cpu()
        if self.bias is not None: self.bias = self.bias.cpu()


    def unswap_cpu(self):
        if self.swap_device is None:
            return
        if self.su is not None: self.su = self.su.to(self.swap_device)
        if self.sv is not None: self.sv = self.sv.to(self.swap_device)
        if self.suh is not None: self.suh = self.suh.to(self.swap_device)
        if self.svh is not None: self.svh = self.svh.to(self.swap_device)
        if self.trellis is not None: self.trellis = self.trellis.to(self.swap_device)
        if self.bias is not None: self.bias = self.bias.to(self.swap_device)
        self.swap_device = None
