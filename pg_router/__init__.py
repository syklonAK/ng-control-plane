"""pg-router: generic configuration-driven Nginx routing control plane.

Python is a control plane only: it configures, validates, monitors and
orchestrates Nginx. Production traffic is always handled by Nginx itself.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
