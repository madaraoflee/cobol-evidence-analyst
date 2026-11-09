"""HTTP shell integration for the isolated online workbench only."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def dispatch_online(server, method, target, payload=None):
    route = urlsplit(target)
    if (route.scheme or route.netloc or route.fragment
            or not route.path.startswith("/api/online/")
            or (route.query and method != "GET")):
        return 400, {"error": "ONLINE_INVALID_ROUTE"}
    try:
        values = parse_qs(route.query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        return 400, {"error": "ONLINE_INVALID_QUERY"}
    if any(len(items) != 1 for items in values.values()):
        return 400, {"error": "ONLINE_INVALID_QUERY"}
    query = {key: items[0] for key, items in values.items()}
    with server.online_lock:
        if server.online_app is None:
            from online_workspace import OnlineWorkbench
            from app_paths import state_root
            parent = Path(server.app.state_path).parent if server.app.state_path else state_root()
            server.online_app = OnlineWorkbench(parent / "online", config_provider=server.app.config_provider)
        controller = server.online_app
    return controller.dispatch(method, route.path, payload=payload, query=query)
