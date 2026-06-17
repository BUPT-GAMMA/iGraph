import torch


def select_best_gpu() -> torch.device:
    """Pick the GPU with the most headroom (free memory + low utilization).

    Priority:
    1. pynvml — combines free-memory ratio and GPU utilization rate.
    2. torch.cuda.mem_get_info — memory-only fallback (no extra deps).
    Falls back to CPU when no GPU is available.
    """
    if not torch.cuda.is_available():
        return torch.device("cpu")

    n = torch.cuda.device_count()
    if n == 1:
        return torch.device("cuda:0")

    # --- pynvml path (memory + utilization) ---
    try:
        import pynvml

        pynvml.nvmlInit()
        best_idx, best_score = 0, -float("inf")
        for i in range(n):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            free_ratio = mem.free / mem.total          # higher = better
            util_ratio = util.gpu / 100.0              # lower = better
            score = free_ratio - util_ratio
            if score > best_score:
                best_score, best_idx = score, i
        pynvml.nvmlShutdown()
        return torch.device(f"cuda:{best_idx}")
    except Exception:
        pass

    # --- torch fallback (memory only) ---
    best_idx, best_free = 0, -1
    for i in range(n):
        try:
            free, _ = torch.cuda.mem_get_info(i)
            if free > best_free:
                best_free, best_idx = free, i
        except Exception:
            pass
    return torch.device(f"cuda:{best_idx}")
