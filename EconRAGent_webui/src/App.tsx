import { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";

const ChatPage = lazy(async () => {
  const module = await import("./pages/ChatPage");
  return { default: module.ChatPage };
});
const GraphPage = lazy(async () => {
  const module = await import("./pages/GraphPage");
  return { default: module.GraphPage };
});
const DiscoverPage = lazy(async () => {
  const module = await import("./pages/DiscoverPage");
  return { default: module.DiscoverPage };
});
const SpacesPage = lazy(async () => {
  const module = await import("./pages/SpacesPage");
  return { default: module.SpacesPage };
});

function PageFallback() {
  return (
    <section className="panel page-loading">
      <div className="empty-state">页面加载中...</div>
    </section>
  );
}

function withSuspense(element: JSX.Element) {
  return <Suspense fallback={<PageFallback />}>{element}</Suspense>;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<AppShell />}>
        <Route index element={<Navigate replace to="/chat" />} />
        <Route path="chat" element={withSuspense(<ChatPage />)} />
        <Route path="graph" element={withSuspense(<GraphPage />)} />
        <Route path="discover" element={withSuspense(<DiscoverPage />)} />
        <Route path="spaces" element={withSuspense(<SpacesPage />)} />
        <Route path="*" element={<Navigate replace to="/chat" />} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter basename="/webui">
      <AppRoutes />
    </BrowserRouter>
  );
}
