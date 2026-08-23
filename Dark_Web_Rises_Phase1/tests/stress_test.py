import asyncio
import time
import argparse
from collections import Counter
import sys

# Ensure the root path is in sys.path so we can import services
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from unittest.mock import MagicMock
sys.modules['torch'] = MagicMock()
sys.modules['open_clip'] = MagicMock()
sys.modules['PIL'] = MagicMock()

from services.ai_handling import get_image, close_http_client, provider_status

async def request_image(prompt: str, task_id: int):
    start = time.perf_counter()
    try:
        # get_image returns a URL path or base64 string, or None if it completely fails
        result = await get_image(prompt)
        latency = time.perf_counter() - start
        if result is None:
            return {"task_id": task_id, "status": "failed", "latency": latency, "error": "Chain returned None"}
        return {"task_id": task_id, "status": "success", "latency": latency, "result_len": len(result)}
    except Exception as e:
        latency = time.perf_counter() - start
        return {"task_id": task_id, "status": "exception", "latency": latency, "error": str(e)}

async def main():
    parser = argparse.ArgumentParser(description="Stress test the image generation API")
    parser.add_argument("--concurrency", type=int, default=100, help="Number of concurrent requests")
    parser.add_argument("--prompt", type=str, default="a futuristic city skyline at sunset, cyberpunk style", help="Prompt to generate")
    args = parser.parse_args()

    print(f"Starting stress test with concurrency={args.concurrency}")
    print(f"Prompt: '{args.prompt}'")
    
    start_time = time.perf_counter()
    
    # Create tasks
    tasks = [request_image(f"{args.prompt} {i}", i) for i in range(args.concurrency)]
    
    # Gather results
    results = await asyncio.gather(*tasks)
    
    total_time = time.perf_counter() - start_time
    
    # Analyze results
    statuses = Counter([r["status"] for r in results])
    latencies = [r["latency"] for r in results if r["status"] == "success"]
    
    print("\n--- Stress Test Results ---")
    print(f"Total time elapsed: {total_time:.2f} seconds")
    print(f"Total requests: {args.concurrency}")
    print(f"Successes: {statuses.get('success', 0)}")
    print(f"Failures (Chain returned None): {statuses.get('failed', 0)}")
    print(f"Exceptions: {statuses.get('exception', 0)}")
    
    if latencies:
        latencies.sort()
        avg_lat = sum(latencies) / len(latencies)
        p50 = latencies[int(len(latencies) * 0.5)]
        p95 = latencies[int(len(latencies) * 0.95)]
        print(f"\nLatency metrics (Successes only):")
        print(f"  Average: {avg_lat:.2f}s")
        print(f"  P50:     {p50:.2f}s")
        print(f"  P95:     {p95:.2f}s")
        print(f"  Min:     {latencies[0]:.2f}s")
        print(f"  Max:     {latencies[-1]:.2f}s")
        
    print("\n--- Provider Status ---")
    stats = provider_status()
    for stat in stats:
        print(f"Provider: {stat['provider']}")
        print(f"  State: {stat['state']}")
        print(f"  Total Successes: {stat['total_successes']}")
        print(f"  Total Failures: {stat['total_failures']}")
        if "observed_cost_usd" in stat:
             print(f"  Cost: ${stat['observed_cost_usd']}")

    # Clean up HTTP client
    await close_http_client()

if __name__ == "__main__":
    asyncio.run(main())
