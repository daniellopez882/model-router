"""model-router: an OpenAI-compatible gateway in front of several model providers.

A client speaks the OpenAI chat-completions API to this service and names a
*route alias* instead of a provider's model. The router picks a provider
according to the route's policy, retries and falls back on the failures that
deserve it, refuses the ones that do not, and accounts every request to a
tenant with a budget.
"""

__version__ = "0.1.0"
