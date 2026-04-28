"""Pilot — daily improvement loop with Feishu-based human-in-the-loop approval.

Orchestrates: scheduled diagnosis → Feishu notification → operator discussion →
approval gate → harness execution → result reporting.
"""
