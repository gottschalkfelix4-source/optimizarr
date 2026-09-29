import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import { Skeleton, Toaster } from "./components/ui";
import { endpoints } from "./lib/api";
import { setSizeUnit } from "./lib/format";

// One chunk per page: the charts library only loads with the pages that draw
// charts, not with the first paint of every page.
const TrashPage = lazy(() => import("./pages/Trash"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const HistoryPage = lazy(() => import("./pages/History"));
const Insights = lazy(() => import("./pages/Insights"));
const Library = lazy(() => import("./pages/Library"));
const MoviesPage = lazy(() => import("./pages/Movies"));
const Queue = lazy(() => import("./pages/Queue"));
const SeriesPage = lazy(() => import("./pages/Series"));
const SettingsPage = lazy(() => import("./pages/Settings"));

function PageFallback() {
  return (
    <div className="space-y-4" aria-busy="true">
      <Skeleton className="h-24" />
      <Skeleton className="h-64" />
    </div>
  );
}

export default function App() {
  const location = useLocation();
  // ``ui.size_unit`` applies everywhere sizes are shown.  Set before the pages
  // render so they format with the right unit in the same pass.
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: endpoints.settings });
  setSizeUnit(settings?.ui?.size_unit);

  return (
    <>
      <Layout>
        <ErrorBoundary resetKey={location.pathname}>
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/library" element={<Library />} />
              <Route path="/series" element={<SeriesPage />} />
              <Route path="/movies" element={<MoviesPage />} />
              <Route path="/queue" element={<Queue />} />
              <Route path="/insights" element={<Insights />} />
              <Route path="/trash" element={<TrashPage />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Dashboard />} />
            </Routes>
          </Suspense>
        </ErrorBoundary>
      </Layout>
      <Toaster />
    </>
  );
}
