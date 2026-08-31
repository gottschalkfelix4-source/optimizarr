import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { LiveProvider, ToastProvider } from "./lib/live";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <LiveProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </LiveProvider>
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
