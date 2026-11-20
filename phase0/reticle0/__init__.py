"""Phase 0 investigation harness for the Sony A7 II (ILCE-7M2) Camera Remote API.

The package is intentionally split so that everything Sony-specific lives in
``reticle0.sony``. Nothing outside that module may hard-code a Sony JSON-RPC
method name, which is the constraint the future Reticle capture engine depends on.
"""

__version__ = "0.1.0"
