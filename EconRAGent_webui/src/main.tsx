import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";

import { App } from "./App";
import { createQueryClient } from "./app/queryClient";
import "./styles.css";

const queryClient = createQueryClient();
const root = document.getElementById("root");

if (!root) {
  throw new Error("Root element #root was not found");
}

ReactDOM.createRoot(root).render(
  <QueryClientProvider client={queryClient}>
    <App />
  </QueryClientProvider>,
);
