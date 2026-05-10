"""Local launcher for the Ray Serve Docling deployment.

Usage:
    python run_serve_local.py              # default port 8100
    python run_serve_local.py --port 9000  # custom port

Starts a local Ray instance and deploys the Docling Serve application.
The endpoint is available at http://127.0.0.1:<port>/ until Ctrl-C.

On RHOAI, this file is NOT used — the RayService CRD manages the
Serve application lifecycle on the cluster.
"""

import argparse
import os

import ray
from ray import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("SERVE_PORT", "8100")))
    args = parser.parse_args()

    ray.init()

    from serve_app import app, health_app

    serve.start(http_options={"port": args.port})
    serve.run(app, name="docling-processor", route_prefix="/")
    serve.run(health_app, name="health", route_prefix="/health")

    print(f"\n=== Ray Serve Docling Processor ===")
    print(f"  Process docs:  POST http://127.0.0.1:{args.port}/")
    print(f"  Health check:  GET  http://127.0.0.1:{args.port}/health")
    print(f"  Dashboard:     http://127.0.0.1:8265/")
    print(f"  Press Ctrl-C to shut down\n")

    try:
        import signal
        signal.pause()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        serve.shutdown()
        ray.shutdown()
