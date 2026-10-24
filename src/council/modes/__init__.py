"""The mode registry: importing this package registers every mode."""
from .base import REGISTRY, BaseAdapter, adapter_for, modes, register
from .plan import PLAN
from .review import REVIEW

__all__ = ["REGISTRY", "BaseAdapter", "adapter_for", "modes", "register", "REVIEW", "PLAN"]
