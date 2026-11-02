"""A small OTLP collector over HTTP. It prints the name and the attribute
names of each span it gets. start.sh starts it on TOUR_OTLP_PORT.
    collector.py PORT"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


class Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        for rs in ExportTraceServiceRequest.FromString(body).resource_spans:
            for ss in rs.scope_spans:
                for span in ss.spans:
                    keys = sorted(kv.key for kv in span.attributes)
                    print(self.path, span.name, keys, flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Collector).serve_forever()
