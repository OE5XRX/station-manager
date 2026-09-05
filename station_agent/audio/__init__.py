"""Audio subsystem for the Station Agent.

Implements the Session-B slice of the OE5XRX audio design: PipeWire graph control,
per-stream Opus bridging, the outbound audio-WebSocket client, and the audio-router
virtual control-plane module. See:

- docs/superpowers/specs/2026-09-03-audio-subsystem-design.md (Spec 0, NORMATIVE wire contract)
- docs/superpowers/specs/2026-09-05-audio-agent-component-design.md (this session's component design)
"""
