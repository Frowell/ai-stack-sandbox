"""ILLUSTRATIVE target for app/agent.py's generate_node — a spec, not wired in.

The only change is documentary: the span keeps `gen_ai.request.model: "chat"`
(the requested alias) and now ALSO carries `gen_ai.response.model`, set from
inside gateway.chat() (see gateway_served_model.py). No structural change here —
this snippet exists to show that standby activation becomes queryable in any OTel
backend by filtering generate spans on gen_ai.response.model.

Before:
    with span("generate", **{"gen_ai.operation.name": "chat",
                             "gen_ai.request.model": "chat"}):
        answer = chat([...])
    # span had only the hardcoded alias; you could not tell which deployment served it.

After (no code change in this node — chat() annotates the active span):
    with span("generate", **{"gen_ai.operation.name": "chat",
                             "gen_ai.request.model": "chat"}):
        answer = chat([...])         # gateway sets gen_ai.response.model = "chat-bedrock"
    # span now distinguishes requested alias ("chat") from served deployment.
"""
# (no runnable code — see gateway_served_model.py for the actual change)
