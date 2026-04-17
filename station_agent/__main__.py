"""Entry point for running the Station Agent as a module.

Usage: python -m station_agent
"""

from .agent import StationAgent

if __name__ == "__main__":
    agent = StationAgent()
    agent.run()
