import asyncio
import os
import time
import argparse
import httpx
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

async def test_deepinfra_worker(client, api_key, model, task_id):
    url = f"https://api.deepinfra.com/v1/inference/{model}"
    start = time.perf_counter()
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"prompt": f"a simple test image of a red apple, variation {task_id}", "width": 512, "height": 512},
            timeout=30.0
        )
        latency = time.perf_counter() - start
        if response.status_code == 200:
            return {"status": "success", "latency": latency}
        else:
            return {"status": "failed", "latency": latency, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "exception", "latency": time.perf_counter() - start, "error": str(e)}

async def test_replicate_worker(client, api_key, model, task_id):
    url = f"https://api.replicate.com/v1/models/{model}/predictions"
    start = time.perf_counter()
    try:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Prefer": "wait"
            },
            json={"input": {"prompt": f"a simple test image of a blue sky, variation {task_id}"}},
            timeout=30.0
        )
        latency = time.perf_counter() - start
        if response.status_code in (200, 201):
            payload = response.json()
            if payload.get("status") == "succeeded":
                return {"status": "success", "latency": latency}
            else:
                return {"status": "failed", "latency": latency, "error": f"Status: {payload.get('status')}"}
        else:
            return {"status": "failed", "latency": latency, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "exception", "latency": time.perf_counter() - start, "error": str(e)}

async def test_pollinations_worker(client, model, base_url, task_id):
    prompt = f"a simple test image of a green forest {task_id}"
    url = f"{base_url}/{prompt.replace(' ', '%20')}?model={model}&width=512&height=512&nologo=true"
    start = time.perf_counter()
    try:
        response = await client.get(url, timeout=30.0)
        latency = time.perf_counter() - start
        if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
            return {"status": "success", "latency": latency}
        else:
            return {"status": "failed", "latency": latency, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "exception", "latency": time.perf_counter() - start, "error": str(e)}


def print_results(provider_name, results, total_time):
    successes = [r for r in results if r["status"] == "success"]
    failures = [r for r in results if r["status"] == "failed"]
    exceptions = [r for r in results if r["status"] == "exception"]
    
    print(f"\n--- {provider_name} Results ---")
    print(f"Total time: {total_time:.2f}s")
    print(f"Total requests: {len(results)}")
    print(f"Successes: {len(successes)}")
    print(f"Failures (HTTP Errors, etc): {len(failures)}")
    print(f"Exceptions (Timeouts, Network): {len(exceptions)}")
    
    if successes:
        latencies = sorted([r["latency"] for r in successes])
        print(f"Latency (Successes only):")
        print(f"  Avg: {sum(latencies)/len(latencies):.2f}s")
        print(f"  Min: {latencies[0]:.2f}s")
        print(f"  Max: {latencies[-1]:.2f}s")
        print(f"  P50: {latencies[int(len(latencies)*0.5)]:.2f}s")
        print(f"  P95: {latencies[int(len(latencies)*0.95)]:.2f}s")
    
    if failures or exceptions:
        print("\nSample Errors:")
        error_samples = (failures + exceptions)[:5]
        for e in error_samples:
            print(f"  - {e.get('error')}")


async def main():
    parser = argparse.ArgumentParser(description="Directly stress test 3rd party APIs")
    parser.add_argument("--concurrency", type=int, default=40, help="Number of concurrent requests per provider")
    args = parser.parse_args()

    print(f"Starting Direct API Stress Tests (Concurrency: {args.concurrency})...\n")

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=args.concurrency * 2)) as client:
        
        # 1. DeepInfra
        """ api_key = os.getenv("DEEPINFRA_API_KEY")
        if not api_key:
            print("[!] DEEPINFRA_API_KEY not found in .env (Skipping)")
        else:
            model = os.getenv("DEEPINFRA_MODEL", "black-forest-labs/FLUX-2-klein-4b")
            print(f"\nSending {args.concurrency} concurrent requests to DeepInfra ({model})...")
            start = time.perf_counter()
            tasks = [test_deepinfra_worker(client, api_key, model, i) for i in range(args.concurrency)]
            results = await asyncio.gather(*tasks)
            print_results("DeepInfra", results, time.perf_counter() - start) """

        # 2. Replicate
        api_key = os.getenv("REPLICATE_API_TOKEN")
        if not api_key:
            print("\n[!] REPLICATE_API_TOKEN not found in .env (Skipping)")
        else:
            model = os.getenv("REPLICATE_MODEL", "black-forest-labs/flux-schnell")
            print(f"\nSending {args.concurrency} concurrent requests to Replicate ({model})...")
            start = time.perf_counter()
            tasks = [test_replicate_worker(client, api_key, model, i) for i in range(args.concurrency)]
            results = await asyncio.gather(*tasks)
            print_results("Replicate", results, time.perf_counter() - start)

        # 3. Pollinations
        """ model = os.getenv("POLLINATIONS_MODEL", "flux")
        base_url = os.getenv("POLLINATIONS_BASE", "https://image.pollinations.ai/p")
        print(f"\nSending {args.concurrency} concurrent requests to Pollinations (Free Tier)...")
        start = time.perf_counter()
        tasks = [test_pollinations_worker(client, model, base_url, i) for i in range(args.concurrency)]
        results = await asyncio.gather(*tasks)
        print_results("Pollinations", results, time.perf_counter() - start) """

    print("\nDirect API Stress Tests complete.")

if __name__ == "__main__":
    asyncio.run(main())
