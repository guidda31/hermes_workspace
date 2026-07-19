"""LLM (Hermes) decision layer: deterministic tools the agent invokes.

This package contains NO LLM API client. Hermes is the brain; these modules only
build point-in-time briefs, validate the agent's structured decisions, apply hard
guardrails, and record signal audits. No order-submission or network code lives here.
"""
