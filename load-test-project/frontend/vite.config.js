import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API (api/app.py) listens on 8800; proxying keeps the browser on one origin.
export default defineConfig({
    plugins: [react()],
    server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8800" } },
});