/** @type {import('next').NextConfig} */
const nextConfig = {
  rewrites: async () => {
    return [
      {
        source: "/api/py/:path*",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/api/py/:path*"
            : "/api/py/:path*",
      },
      {
        source: "/crawl",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/crawl"
            : "/api/py/crawl",
      },
      {
        source: "/vector-store/info",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/vector-store/info"
            : "/api/py/vector-store/info",
      },
      {
        source: "/vector-store/search",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/vector-store/search"
            : "/api/py/vector-store/search",
      },
      {
        source: "/status/:taskId",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/status/:taskId"
            : "/api/py/status/:taskId",
      },
      {
        source: "/ws/:taskId",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/ws/:taskId"
            : "/api/py/ws/:taskId",
      },
      {
        source: "/docs",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/api/py/docs"
            : "/api/py/docs",
      },
      {
        source: "/openapi.json",
        destination:
          process.env.NODE_ENV === "development"
            ? "http://127.0.0.1:8000/api/py/openapi.json"
            : "/api/py/openapi.json",
      },
    ];
  },
};

module.exports = nextConfig;
