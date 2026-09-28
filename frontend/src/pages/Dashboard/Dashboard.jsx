import React from "react";
import ChartOne from "../../components/Charts/ChartOne";

import TableOne from "../../components/Tables/TableOne";

import CardDataStatsCum from "./CardDataStatsCum";
import CardDataStatsCumulative from "./CardDataStatsCumulative";
import CardDataStatsWeek from "./CardDataStatsWeek";
import CardDataStatsYear from "./CardDataStatsYear";
import CardDataStatsYearCosts from "./CardDataStatsYearCosts";
import CardLastPhoto from "./CardLastPhoto";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import { useState } from "react";
import Loader from "../../common/Loader";

const fetcher = async (url) => fetch(url).then((res) => res.json());

const Dashboard = () => {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  const url = "http://" + hostname + ":8000/api/v1/readings/last";

  const { data, error, isLoading } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  const settingsUrl = "http://" + hostname + ":8000/api/v1/settings";
  const { data: settings } = useSWR(settingsUrl, fetcher, {});

  if (isLoading) return <Loader />;

  if (error)
    return (
      <div className="flex h-40 items-center justify-center rounded-sm border border-stroke bg-white px-4 text-center shadow-default dark:border-strokedark dark:bg-boxdark">
        <p className="text-body dark:text-bodydark">
          {t("measurementinprogress")}
        </p>
      </div>
    );

  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6 xl:grid-cols-5 2xl:gap-7.5">
        <CardDataStatsWeek />
        <CardDataStatsYear />
        <CardDataStatsYearCosts />
        <CardDataStatsCum />
        <CardDataStatsCumulative />
      </div>

      <div className="mt-4 grid grid-cols-12 gap-4 md:mt-6 md:gap-6 2xl:mt-7.5 2xl:gap-7.5">
        <ChartOne />
        <div className="col-span-12 xl:col-span-12">
          <TableOne />
        </div>
        {settings?.developerMode && <CardLastPhoto />}
      </div>
    </>
  );
};

export default Dashboard;
