"""AI Assist module — Ollama-powered clinical note generation.

Provides a standalone floating window that reads grabbed EMR data from the
bridge JSON file, de-identifies it, sends it to a local Ollama instance,
and lets the user edit and insert AI-generated text into the EMR.
"""
