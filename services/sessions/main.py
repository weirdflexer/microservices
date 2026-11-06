from common.runtime import make_app
from . import Service, make_router


def build_app(context=None, background=True):
    return make_app("sessions", Service, make_router, context=context, background=background)


app = build_app()
