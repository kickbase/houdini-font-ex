try:
    kwargs['node'].hdaModule().on_loaded(**kwargs)
except Exception:
    pass
