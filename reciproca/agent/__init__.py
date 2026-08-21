"""The LangChain agent frontend (milestone M4).

The agent is a frontend like the CLI and the GUI are: it talks to the MCP
server over stdio and drives the same cycles. The engine still decides every
individual follow; the model decides what to run and monitors the run.
"""
