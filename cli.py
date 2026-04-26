#!/usr/bin/env python3
"""Aetheer v3 CLI — Independent Autonomous Orchestrator.

Allows running full cognitive analysis without Claude CLI.
"""
from __future__ import annotations

import asyncio
import argparse
import sys
import os
import time
from uuid import uuid4
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path
root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir))

# Load environment variables
load_dotenv(root_dir / ".env")

from agents.cognitive_agent import AetheerCognitiveAgent, CognitiveDeps
from agents.openrouter_client import OpenRouterClient
from agents.model_router import AetheerModelRouter
from agents.schemas import CognitiveQuery, RequestedBy
from services.cost_monitor import CostMonitor
from interfaces.mcp_bridge import StandardMcpBridge

# Optional Rich UI
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.live import Live
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.markdown import Markdown
    from rich.table import Table
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

async def main():
    parser = argparse.ArgumentParser(description="Aetheer Cognitive CLI")
    parser.add_argument("query", nargs="?", help="Market analysis query")
    parser.add_argument("--intent", default="full_analysis", help="Query intent")
    parser.add_argument("--trace-id", help="Custom trace ID")
    parser.add_argument("--budget", type=float, default=10.0, help="Daily budget cap USD")
    
    args = parser.parse_args()
    
    query_text = args.query
    if not query_text:
        if HAS_RICH:
            query_text = console.input("[bold blue]Aetheer > [/bold blue]")
        else:
            query_text = input("Aetheer > ")
            
    if not query_text:
        return

    # Setup dependencies
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: OPENROUTER_API_KEY env var missing.")
        sys.exit(1)
        
    root = Path(__file__).resolve().parent
    db_path = root / "db" / "aetheer.db"
    
    mcp = StandardMcpBridge()
    client = OpenRouterClient(api_key=api_key)
    router = AetheerModelRouter()
    cost_monitor = CostMonitor(db_path=db_path)
    
    deps = CognitiveDeps(
        client=client,
        router=router,
        cost_monitor=cost_monitor,
        mcp=mcp
    )
    
    agent = AetheerCognitiveAgent(deps)
    
    trace_id = args.trace_id or f"cli-{uuid4().hex[:8]}"
    
    query = CognitiveQuery(
        query_text=query_text,
        query_intent=args.intent, # type: ignore
        instruments=["EURUSD", "GBPUSD", "DXY"], # Default basket
        timeframes=["H1", "H4"],
        requested_by="user",
        trace_id=trace_id
    )

    if HAS_RICH:
        console.print(Panel(f"Query: [bold]{query_text}[/bold]\nTrace: [dim]{trace_id}[/dim]", title="Aetheer v3 Orchestrator"))
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            task = progress.add_task("Running cognitive analysis...", total=None)
            try:
                response = await agent.cognitive_analysis(query)
            except Exception as e:
                progress.stop()
                console.print(f"[bold red]Critical Error:[/bold red] {e}")
                return
    else:
        print(f"Analyzing: {query_text} (Trace: {trace_id})...")
        response = await agent.cognitive_analysis(query)

    # Output result
    if HAS_RICH:
        if response.approved and response.synthesis_text:
            console.print(Markdown(response.synthesis_text))
            
            # Show stats
            table = Table(title="Analysis Metadata", box=None)
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="magenta")
            table.add_row("Operating Mode", response.operating_mode)
            table.add_row("Quality Score", f"{response.quality.global_score:.2f}")
            table.add_row("Cost (USD)", f"${response.cost_usd:.4f}")
            table.add_row("Latency (ms)", f"{response.latency_ms}ms")
            if response.attention:
                table.add_row("Dominant Theme", response.attention.dominant_theme)
            if response.regime:
                table.add_row("Market Regime", response.regime.classification)
            
            console.print(table)
        else:
            console.print(Panel(f"[bold red]Rejected:[/bold red] {response.rejection_reason}", title="Governor Verdict"))
            if response.quality:
                console.print(f"Quality: {response.quality.global_score:.2f}")
    else:
        if response.approved:
            print("\n--- ANALYSIS ---\n")
            print(response.synthesis_text)
            print(f"\nMode: {response.operating_mode} | Quality: {response.quality.global_score:.2f} | Cost: ${response.cost_usd:.4f}")
        else:
            print(f"\nREJECTED: {response.rejection_reason}")

    # Cleanup
    await mcp.aclose()
    await client.aclose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAborted.")
