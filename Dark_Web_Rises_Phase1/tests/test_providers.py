import sys
import os
import time
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from services.providers import CircuitBreaker, ProviderChain, CLOSED, OPEN, HALF_OPEN, ProviderError

def test_circuit_breaker_state_transitions():
    cb = CircuitBreaker("test", threshold=2, cooldown=0.1)
    assert cb.state == CLOSED

    # First failure
    cb.record_failure()
    assert cb.state == CLOSED
    assert cb.consecutive_failures == 1

    # Second failure -> OPEN
    cb.record_failure()
    assert cb.state == OPEN
    assert cb.allow_request() is False

    # Wait for cooldown
    time.sleep(0.15)
    
    # After cooldown, should allow exactly one probe
    assert cb.allow_request() is True
    assert cb.state == HALF_OPEN
    assert cb._probe_in_flight is True
    
    # Second concurrent request should be blocked while probe is in flight
    assert cb.allow_request() is False

    # Probe succeeds -> CLOSED
    cb.record_success()
    assert cb.state == CLOSED
    assert cb.consecutive_failures == 0

def test_circuit_breaker_probe_failure():
    cb = CircuitBreaker("test", threshold=1, cooldown=0.1)
    cb.record_failure()
    assert cb.state == OPEN
    
    time.sleep(0.15)
    assert cb.allow_request() is True
    assert cb.state == HALF_OPEN
    
    # Probe fails -> OPEN again
    cb.record_failure()
    assert cb.state == OPEN
    assert cb.allow_request() is False

def test_circuit_breaker_release_probe():
    cb = CircuitBreaker("test", threshold=1, cooldown=0.1)
    cb.record_failure()
    time.sleep(0.15)
    assert cb.allow_request() is True
    
    # Request is cancelled, we release the probe
    cb.release_probe()
    assert cb.state == HALF_OPEN
    assert cb._probe_in_flight is False
    
    # New probe is allowed
    assert cb.allow_request() is True

async def test_provider_chain_fallback():
    class DummyProvider1:
        def __init__(self):
            self.name = "dummy1"
            self.breaker = CircuitBreaker(self.name, threshold=1, cooldown=1.0)
        
        async def generate(self, client, prompt):
            raise ProviderError("simulated failure")
            
    class DummyProvider2:
        def __init__(self):
            self.name = "dummy2"
            self.breaker = CircuitBreaker(self.name, threshold=1, cooldown=1.0)
        
        async def generate(self, client, prompt):
            return b"success"

    p1 = DummyProvider1()
    p2 = DummyProvider2()
    chain = ProviderChain([p1, p2])
    
    # Should fail on p1, succeed on p2
    result = await chain.generate(None, "test")
    assert result == b"success"
    
    assert p1.breaker.state == OPEN
    assert p2.breaker.state == CLOSED
    
    # Next request should skip p1 immediately and succeed on p2
    result = await chain.generate(None, "test2")
    assert result == b"success"

async def main():
    test_circuit_breaker_state_transitions()
    test_circuit_breaker_probe_failure()
    test_circuit_breaker_release_probe()
    await test_provider_chain_fallback()
    print("All test_providers tests passed!")

if __name__ == "__main__":
    asyncio.run(main())

