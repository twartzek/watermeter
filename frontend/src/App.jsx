import { useEffect, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";

import Loader from "./common/Loader";
import PageTitle from "./components/PageTitle";
import Dashboard from "./pages/Dashboard/Dashboard";
import Profile from "./pages/Profile";
import Settings from "./pages/Settings";
import DefaultLayout from "./layout/DefaultLayout";

function App() {
  const [loading, setLoading] = useState(true);
  const { pathname } = useLocation();

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);

  useEffect(() => {
    setTimeout(() => setLoading(false), 1000);
  }, []);

  return loading ? (
    <div className="flex h-screen items-center justify-center bg-white">
      <Loader />
    </div>
  ) : (
    <DefaultLayout>
      <Routes>
        <Route
          path="/"
          element={
            <>
              <PageTitle title="Dashboard | WatermeterAI NextGen" />
              <Dashboard />
            </>
          }
        />
        <Route
          path="/dashboard"
          element={
            <>
              <PageTitle title="Dashboard | WatermeterAI NextGen" />
              <Dashboard />
            </>
          }
        />
        <Route
          path="/about"
          element={
            <>
              <PageTitle title="About | WatermeterAI NextGen" />
              <Profile />
            </>
          }
        />
        <Route
          path="/settings"
          element={
            <>
              <PageTitle title="Settings | WatermeterAI NextGen" />
              <Settings />
            </>
          }
        />
      </Routes>
    </DefaultLayout>
  );
}

export default App;
