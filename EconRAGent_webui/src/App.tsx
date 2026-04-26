import { Suspense, lazy, useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";

const loadChatPage = async () => {
  const module = await import("./pages/ChatPage");
  return { default: module.ChatPage };
};
const loadGraphPage = async () => {
  const module = await import("./pages/GraphPage");
  return { default: module.GraphPage };
};
const loadDiscoverPage = async () => {
  const module = await import("./pages/DiscoverPage");
  return { default: module.DiscoverPage };
};
const loadSpacesPage = async () => {
  const module = await import("./pages/SpacesPage");
  return { default: module.SpacesPage };
};
const preloadGraphRenderer = async () => import("./components/CytoscapeGraph");

const ChatPage = lazy(loadChatPage);
const GraphPage = lazy(loadGraphPage);
const DiscoverPage = lazy(loadDiscoverPage);
const SpacesPage = lazy(async () => {
  return loadSpacesPage();
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
  useEffect(() => {
    void loadChatPage();
    void loadGraphPage();
    void loadDiscoverPage();
    void preloadGraphRenderer();
  }, []);

  return (
    <BrowserRouter basename="/webui">
      <AppRoutes />
    </BrowserRouter>
  );
}
