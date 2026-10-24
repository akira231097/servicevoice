"""Isolated phone ceilings; Vapi's four slots are shared with the main agent.

Concurrency is callers in separate rooms, never participants in one room.
The finite lifetime prototype quota remains separate from concurrent capacity.
"""

MAX_CONCURRENT_CALLS = 4
MAX_PROTOTYPE_CALLS = 60
