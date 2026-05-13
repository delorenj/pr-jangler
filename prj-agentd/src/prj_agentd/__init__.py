"""prj-agentd: Mission Control daemon for PR Jangler.

PR Jangler owns the deterministic state machine (queue, phases, run-log).
prj-agentd owns the runtime control plane: app-server thread persistence,
approval policy, event mirroring, and the heartbeat loop.

Layers:
    GitHub reality
      v
    PR Jangler deterministic queue/state/log layer
      v
    app-server thread/turn/item runtime layer
      v
    approval + policy layer (prj_agentd.policy)
      v
    dashboard / mission-control layer (prj_agentd.timeline)
"""

__version__ = "0.1.0"
