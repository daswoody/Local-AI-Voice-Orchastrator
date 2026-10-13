"""Minimaler torch-Ersatz fuer Tests: nur was die dtype-Wahl braucht."""

import os

float32 = "torch.float32"
float16 = "torch.float16"
bfloat16 = "torch.bfloat16"


class cuda:  # noqa: N801 - wie torch.cuda
    @staticmethod
    def get_device_capability(index: int = 0) -> tuple[int, int]:
        major, minor = os.environ.get("FAKE_CUDA_CAPABILITY", "7.5").split(".")
        return int(major), int(minor)
