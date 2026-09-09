"""Private, query-verified tabular construction workers owned by Graph Engine.

No worker tools, messages or memories are registered with the main agent.
"""


def register_builtin_algorithms(store):
    """Install workers at the existing Path/options -> graph extension point."""
    from graph_engine.construction.agent import TabularConstructionSubAgent

    # The callable creates all mutable state per invocation, including its LLM.
    for kind in ("csv", "sql"):
        store.register_algorithm(kind, TabularConstructionSubAgent(data_root=store.root))
        store._process_algorithms.add(kind)
    return store
