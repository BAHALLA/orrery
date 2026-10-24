"""The Studio demo's helpers, shipped in the image (AEP-031).

- :mod:`.writer`: a plain a2a-sdk "writer" agent, the demo's remote in
  another framework (``python -m orrery_core.studio.demo.writer``);
- :mod:`.certs`: a throwaway internal CA and the writer's certificate
  (``python -m orrery_core.studio.demo.certs``).

Neither is imported by the server; a deployment runs them from the image.
"""
