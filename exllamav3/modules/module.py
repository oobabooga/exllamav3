from __future__ import annotations
from abc import ABC, abstractmethod
import torch
import os
import torch.nn.functional as F
from torch import nn
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..models import Config

# Use host bounce when moving state from device to device in layer split
no_p2p_copy = os.environ.get('EXLLAMA_NO_P2P_COPY', None)

class Module(ABC):

    def __init__(
        self,
        config: Config,
        key: str,
        qmap: str | None,
    ):
        """
        :param config:
            Model config

        :param key:
            Tensor key, reflects name in .safetensors collection

        :param qmap:
            Label for the hidden state upon entry into the forward function. Used to collect states/Hessian data
            in linear layers during quantization, e.g. to allow sharing between Q/K/V projections that have the same
            input state.
        """
        self.config = config
        self.key = key
        self.alt_key = None
        self.used_alt_key = False
        self.device = None
        self.modules = []
        self.caps = {}
        self.qmap = qmap
        self.num_slices = 1
        self.qbits_mod_key = ""

    def __iter__(self):
        yield self
        for module in self.modules:
            yield from module

    def find_module(self, key: str):
        for module in self:
            if module.key == key:
                return module

    def can_defer_load(self):
        if len(self.modules) == 0: return True
        return all(module.can_defer_load() for module in self.modules)

    def load(self, device: torch.Device, **kwargs):
        self.device = device
        for module in self.modules:
            module.load(device, **kwargs)

    def unload(self):
        self.device = None
        for module in self.modules:
            module.unload()

    def prepare_for_device(self, x: torch.Tensor, params: dict) -> torch.Tensor:
        global no_p2p_copy
        if x.device != self.device:
            if no_p2p_copy:
                print(".")
                x = x.cpu().to(self.device)
            else:
                x = x.to(self.device)
        return x

    def get_qmaps(self):
        sq = set()
        if self.qmap:
            sq.add(self.qmap)
        for m in self.modules:
            sq.update(m.get_qmaps())
        return sq

    def get_tensors(self):
        return {}

    def weights_numel(self):
        return sum(m.weights_numel() for m in self.modules)

    @abstractmethod
    def forward(
        self,
        x: torch.Tensor,
        params: dict,
        out_dtype: torch.dtype
    ) -> torch.Tensor:
        pass

    def allocate_q(self, quant_args: dict, surplus_bits: int):
        return {}, surplus_bits

    def register_submodule(self, module: Module | None):
        if module is not None:
            self.modules.append(module)

    def quant_format_id(self):
        return None

    def get_name(self):
        return self.__class__.__name__