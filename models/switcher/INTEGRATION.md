"""
Integration Guide: Model Switcher + Model Router
================================================

The Model Switcher (models/switcher/) integrates with ModelRouter via:

1. ModelSwitcher holds a reference to ModelRouter instance
2. When switch() is called, ModelSwitcher updates its internal state
3. ModelRouter can query the switcher for the preferred provider

MODIFICATIONS REQUIRED IN model_router.py:
"""

# Add to model_router.py after the imports section:

# -------------------------------------------------------------------------
# Model Switcher Integration
# -------------------------------------------------------------------------

def set_active_provider(router: ModelRouter, provider_name: str) -> None:
    """
    Called by ModelSwitcher.switch() to update the router's active provider.
    
    This does NOT change the routing table — it updates which provider
    the router should prefer when multiple options are available.
    
    For example, if user switches to "deepseek", the router will:
    - Still use its fallback chain internally
    - But will attempt "local" provider (deepseek) first for OFFLINE tasks
    """
    if provider_name not in router._providers:
        log.warning("set_active_provider: provider not found in router", provider=provider_name)
        return
    
    # For local providers, ensure provider status is refreshed
    provider = router._providers[provider_name]
    provider.ensure_ready()  # Reset OFFLINE status if model is now available
    
    log.info("Active provider updated", provider=provider_name)


def get_active_provider_from_switcher() -> str:
    """
    Returns the currently active provider from the ModelSwitcher.
    
    Used by components that need to know which model is active.
    Falls back to "groq" if switcher not initialised.
    """
    try:
        from models.switcher.model_switcher import ModelSwitcher
        switcher = ModelSwitcher.get_instance()
        return switcher.current_provider
    except Exception:
        return "groq"


# In ModelRouter.complete() or stream(), you can use:
# preferred = get_active_provider_from_switcher()
# to influence routing decisions

# -------------------------------------------------------------------------
# Example HUD Integration
# -------------------------------------------------------------------------

# Client-side JavaScript:
# fetch('/api/model/switch', {
#   method: 'POST',
#   headers: {'Content-Type': 'application/json'},
#   body: JSON.stringify({provider: 'openvino', model: 'qwen_openvino'})
# })

# Server endpoint (add to server.py):
# @app.post("/api/model/switch")
# async def switch_model(req: Request):
#     data = await req.json()
#     provider = data.get("provider", "groq")
#     model = data.get("model")
#     success = await model_switcher.switch(provider, model)
#     return {"success": success, "provider": provider, "model": model}

# -------------------------------------------------------------------------
# Example usage from Python:
# -------------------------------------------------------------------------
# from models.switcher import ModelSwitcher, get_router
# 
# switcher = ModelSwitcher(router=get_router())
# await switcher.switch("openvino", "qwen_openvino")  # Switch to local Qwen OpenVINO
# await switcher.switch("groq")  # Instant switch to Groq
# print(switcher.get_state())  # {'provider': 'groq', 'model': 'auto', ...}
# await switcher.cycle_to_next()  # Next in rotation