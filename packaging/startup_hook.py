"""First custom runtime hook; bootloader work before this point is unobserved."""

from startup_trace import emit

emit("bootstrap")
