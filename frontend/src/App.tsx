import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Toaster } from "./components/ui";
import Dashboard from "./pages/Dashboard";
import HistoryPage from "./pages/History";
import Insights from "./pages/Insights";
import Library from "./pages/Library";
import Queue from "./pages/Queue";
import SettingsPage from "./pages/Settings";

export default function App() {
  return (
    <>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/library" element={<Library />} />
          <Route path="/queue" element={<Queue />} />
          <Route path="/insights" element={<Insights />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Dashboard />} />
        </Routes>
      </Layout>
      <Toaster />
    </>
  );
}
