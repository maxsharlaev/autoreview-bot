"""Model-API egress proxy (M3): job tokens in, real provider keys only here."""

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        from app.model_proxy.main import app

        return app
    raise AttributeError(name)
