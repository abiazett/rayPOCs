"""Ray Serve deployment for real-time document processing with Docling.

Portable module — usable by:
  - `python run_serve_local.py` (local dev)
  - `serve run serve_app:app` (Ray Serve CLI)
  - RayService CRD on RHOAI (serveConfigV2 import path)

Configuration via environment variables:
  INPUT_BASE_DIR   — base directory for resolving relative file paths
                     (local: any dir; cluster: PVC mount like /mnt/data/input)
  OUTPUT_BASE_DIR  — where to write converted markdown/json files
                     (set empty to skip writing output files)
  NUM_REPLICAS     — number of Serve replicas (default: 1)
  NUM_CPUS         — CPUs per replica (default: 2)
  WRITE_JSON       — whether to write JSON output (default: true)
"""

import io
import os
import time

from ray import serve
from starlette.requests import Request
from starlette.responses import JSONResponse

NUM_REPLICAS = int(os.environ.get("NUM_REPLICAS", "1"))
NUM_CPUS = int(os.environ.get("NUM_CPUS", "2"))


@serve.deployment(
    name="docling-processor",
    num_replicas=NUM_REPLICAS,
    ray_actor_options={"num_cpus": NUM_CPUS},
    max_ongoing_requests=1,
)
class DoclingServeDeployment:

    def __init__(self):
        cpus = int(os.environ.get("NUM_CPUS", "2"))
        os.environ["OMP_NUM_THREADS"] = str(cpus)
        os.environ["MKL_NUM_THREADS"] = str(cpus)

        self.input_base_dir = os.environ.get("INPUT_BASE_DIR", "")
        self.output_base_dir = os.environ.get("OUTPUT_BASE_DIR", "")
        self.write_json = os.environ.get("WRITE_JSON", "true").lower() in (
            "1", "true", "yes",
        )

        t_start = time.time()

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorOptions,
            PdfPipelineOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        self._DocumentStream = __import__(
            "docling.datamodel.base_models", fromlist=["DocumentStream"]
        ).DocumentStream

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        pipeline_options.do_table_structure = True
        pipeline_options.accelerator_options = AcceleratorOptions(
            num_threads=cpus,
            device="cpu",
        )
        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )

        self._init_duration = round(time.time() - t_start, 3)
        self._request_count = 0
        self._error_count = 0

        if self.write_json:
            import orjson  # noqa: F401
            self._orjson = orjson

        print(f"[DoclingServe] Replica ready in {self._init_duration}s "
              f"(cpus={cpus}, output={'enabled' if self.output_base_dir else 'disabled'})")

    def _resolve_path(self, file_path: str) -> str:
        if os.path.isabs(file_path):
            return file_path
        if self.input_base_dir:
            return os.path.join(self.input_base_dir, file_path)
        return file_path

    def _write_output(self, fname_base: str, md_bytes: bytes, json_bytes: bytes | None):
        if not self.output_base_dir:
            return
        md_dir = os.path.join(self.output_base_dir, "markdown")
        os.makedirs(md_dir, exist_ok=True)
        with open(os.path.join(md_dir, f"{fname_base}.md"), "wb") as f:
            f.write(md_bytes)
        if json_bytes and self.write_json:
            json_dir = os.path.join(self.output_base_dir, "json")
            os.makedirs(json_dir, exist_ok=True)
            with open(os.path.join(json_dir, f"{fname_base}.json"), "wb") as f:
                f.write(json_bytes)

    async def __call__(self, request: Request) -> JSONResponse:
        self._request_count += 1
        t_total_start = time.time()

        try:
            body = await request.json()
        except Exception:
            self._error_count += 1
            return JSONResponse(
                {"status": "error", "error": "Invalid JSON body"},
                status_code=400,
            )

        file_path = body.get("file_path", "")
        if not file_path:
            self._error_count += 1
            return JSONResponse(
                {"status": "error", "error": "Missing 'file_path' field"},
                status_code=400,
            )

        resolved_path = self._resolve_path(file_path)
        if not os.path.exists(resolved_path):
            self._error_count += 1
            return JSONResponse(
                {"status": "error", "error": f"File not found: {resolved_path}"},
                status_code=404,
            )

        fname = os.path.basename(resolved_path)
        fname_base = fname.rsplit(".", 1)[0]

        try:
            with open(resolved_path, "rb") as f:
                file_bytes = f.read()

            file_size = len(file_bytes)
            if file_size == 0:
                self._error_count += 1
                return JSONResponse(
                    {"status": "error", "error": "File is empty"},
                    status_code=400,
                )

            t_convert_start = time.time()
            stream = self._DocumentStream(
                name=fname, stream=io.BytesIO(file_bytes)
            )
            result = self.converter.convert(stream)
            doc = result.document
            convert_duration = round(time.time() - t_convert_start, 3)

            pages = getattr(doc, "pages", None)
            page_count = len(pages) if pages is not None else 0

            md_bytes = doc.export_to_markdown().encode("utf-8")
            md_kb = round(len(md_bytes) / 1024, 2)

            json_bytes = None
            json_kb = 0.0
            if self.write_json:
                json_bytes = self._orjson.dumps(doc.export_to_dict())
                json_kb = round(len(json_bytes) / 1024, 2)

            self._write_output(fname_base, md_bytes, json_bytes)

            total_duration = round(time.time() - t_total_start, 3)
            file_size_mb = round(file_size / (1024 * 1024), 3)
            pps = round(page_count / convert_duration, 2) if convert_duration > 0 else 0.0

            return JSONResponse({
                "status": "success",
                "filename": fname,
                "pages": page_count,
                "file_size_mb": file_size_mb,
                "markdown_kb": md_kb,
                "json_kb": json_kb,
                "convert_duration_s": convert_duration,
                "total_duration_s": total_duration,
                "pages_per_second": pps,
            })

        except Exception as e:
            self._error_count += 1
            total_duration = round(time.time() - t_total_start, 3)
            return JSONResponse(
                {
                    "status": "error",
                    "filename": fname,
                    "error": str(e)[:300],
                    "total_duration_s": total_duration,
                },
                status_code=500,
            )


@serve.deployment(name="health", num_replicas=1, ray_actor_options={"num_cpus": 0})
class HealthCheck:

    def __init__(self):
        self._start_time = time.time()

    async def __call__(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "healthy",
            "uptime_s": round(time.time() - self._start_time, 1),
        })


app = DoclingServeDeployment.bind()
health_app = HealthCheck.bind()
