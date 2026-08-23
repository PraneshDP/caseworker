# API package — hand-written ASGI console for the caseworker agent.
#
# There is no FastAPI/Starlette dependency here. Two reasons, in order of
# importance: (1) the project's whole HTTP surface is ~12 endpoints plus one
# event stream, and a raw ASGI callable expresses that in a few hundred lines
# that a reviewer can read end to end; (2) adding a web framework to satisfy a
# demo console would be the kind of complexity this project explicitly avoids.
# uvicorn is already present as the ASGI server, so the runtime cost is zero.

from src.api.runner import RunManager, RunState  # noqa: F401
