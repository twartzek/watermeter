import React, { useMemo, useState, useEffect } from "react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Tooltip,
  Legend,
} from "chart.js";
import { Bar } from "react-chartjs-2";
import annotationPlugin from "chartjs-plugin-annotation";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import { DateTime } from "luxon";
import Loader from "../../common/Loader";

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  Tooltip,
  Legend,
  annotationPlugin
);

const fetcher = async (url) => fetch(url).then((res) => res.json());

const buttonActiveClasses =
  "rounded dark:bg-boxdark shadow-card bg-white py-1 px-3 text-xs font-medium text-black hover:bg-white hover:shadow-card dark:text-white dark:hover:bg-boxdark";
const buttonDeactiveClasses =
  "rounded py-1 px-3 text-xs font-medium text-black hover:bg-white hover:shadow-card dark:text-white dark:hover:bg-boxdark";

const ChartOne = () => {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);
  const [active, setActive] = useState("year");

  const thisYear = DateTime.local({ zone: "utc" }).year;
  const lastYear = thisYear - 1;
  const thisMonth = DateTime.local({ zone: "utc" }).month;

  const url =
    "http://" + hostname + ":8000/api/v1/consumptionpermonth/?year=" + thisYear;

  const urlLastYear =
    "http://" + hostname + ":8000/api/v1/consumptionpermonth/?year=" + lastYear;

  const urlperYear = "http://" + hostname + ":8000/api/v1/consumptionperyear";

  const urlConPerDay =
    "http://" +
    hostname +
    ":8000/api/v1/consumptionperday/?year=" +
    thisYear +
    "&month=" +
    thisMonth;

  const {
    data: dataThisYear,
    error: errorThisYear,
    isLoading: isloadThisYear,
  } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  const {
    data: dataLastYear,
    error: errorLastYear,
    isLoading: isloadLastYear,
  } = useSWR(urlLastYear, fetcher, {
    refreshInterval: 60000,
  });

  const {
    data: dataperYear,
    error: errorperYear,
    isLoading: isloadperYear,
  } = useSWR(urlperYear, fetcher, {
    refreshInterval: 60000,
  });

  const {
    data: dataperDay,
    error: errorperDay,
    isLoading: isloadperDay,
  } = useSWR(urlConPerDay, fetcher, {
    refreshInterval: 60000,
  });

  const notificationsUrl = "http://" + hostname + ":8000/api/v1/notifications";
  const { data: notifications } = useSWR(notificationsUrl, fetcher, {
    refreshInterval: 60000,
  });

  const meterReplacementsUrl =
    "http://" + hostname + ":8000/api/v1/meterreplacements";
  const { data: meterReplacements } = useSWR(meterReplacementsUrl, fetcher, {
    refreshInterval: 60000,
  });

  const isMonth = active === "month";
  const isYear = active === "year";
  const isDay = active === "day";

  if (isloadThisYear || isloadLastYear || isloadperYear || isloadperDay)
    return <Loader />;

  if (errorThisYear || errorLastYear || errorperYear || errorperDay)
    return "Error loading data from backend";

  let categories = [];
  let series = [];

  if (isYear) {
    categories = dataperYear.map((item) => item.year);
    series = [
      {
        name: thisYear,
        data: dataperYear.map((item) => item.consumption),
      },
    ];
  }

  if (isMonth) {
    categories = dataThisYear.map((item) =>
      DateTime.fromFormat(item.yearmonth, "yyyy-MM").toLocaleString({
        month: "short",
      })
    );
    series = [
      {
        name: lastYear,
        data: dataLastYear.map((item) => item.consumption),
      },
      {
        name: thisYear,
        data: dataThisYear.map((item) => item.consumption),
      },
    ];
  }

  if (isDay) {
    categories = dataperDay.map((item) => item.day);
    series = [
      {
        name: DateTime.fromObject({ month: thisMonth }).toLocaleString({
          month: "short",
        }),
        data: dataperDay.map((item) => item.consumption),
      },
    ];
  }

  // Flag the days in the currently shown month that had a leakage or
  // sustained-high-flow (possible pipe burst) warning, so a suspiciously
  // high bar can be visually explained even without switching to the
  // fine-grained readings chart.
  const leakageDayNumbers = new Set(
    Array.isArray(notifications)
      ? notifications
          .filter(
            (item) =>
              (item.i18nIdentifier === "leakdetected" ||
                item.i18nIdentifier === "highflowdetected") &&
              item.type === "warning"
          )
          .map((item) => DateTime.fromISO(item.time))
          .filter((dt) => dt.year === thisYear && dt.month === thisMonth)
          .map((dt) => dt.day)
      : []
  );

  // A meter replacement makes the consumption in the bar chart look like an
  // outlier (or an artificial notch) if it isn't explained -- without a
  // marker, e.g. a yearly bar with an unusually low/high value looks like a
  // data error rather than a new meter.
  const replacementDates = Array.isArray(meterReplacements)
    ? meterReplacements.map((r) => DateTime.fromISO(r.time))
    : [];

  // Chart.js' category scale addresses annotations by the index of the
  // label in the `categories` array (not the label value itself, unlike
  // ApexCharts), so each annotation is built from the same index the bar
  // itself is plotted at.
  const annotations = {};

  if (isDay) {
    dataperDay.forEach((item, index) => {
      if (leakageDayNumbers.has(item.day)) {
        annotations[`leakage-${index}`] = {
          type: "label",
          xValue: index,
          yValue: item.consumption,
          content: t("possibleleakage"),
          color: "#fff",
          backgroundColor: "#FB5454",
          font: { size: 10 },
          yAdjust: -16,
        };
      }
      const hasReplacement = replacementDates.some(
        (dt) => dt.year === thisYear && dt.month === thisMonth && dt.day === item.day
      );
      if (hasReplacement) {
        annotations[`replacement-${index}`] = {
          type: "label",
          xValue: index,
          yValue: item.consumption,
          content: t("meterreplacement"),
          color: "#fff",
          backgroundColor: "#B45309",
          font: { size: 10, weight: 600 },
          yAdjust: -16,
        };
      }
    });
  }

  if (isYear && dataperYear) {
    dataperYear.forEach((item, index) => {
      const hasReplacement = replacementDates.some((dt) => dt.year === item.year);
      if (hasReplacement) {
        annotations[`replacement-${index}`] = {
          type: "label",
          xValue: index,
          yValue: item.consumption,
          content: t("meterreplacement"),
          color: "#fff",
          backgroundColor: "#B45309",
          font: { size: 10, weight: 600 },
          yAdjust: -16,
        };
      }
    });
  }

  const chartData = {
    labels: categories,
    datasets: series.map((s, index) => ({
      label: s.name,
      data: s.data,
      backgroundColor: ["#80CAEE", "#3C50E0"][index],
      borderRadius: 2,
      maxBarThickness: 24,
    })),
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: true,
        position: "top",
        align: "start",
      },
      tooltip: {
        enabled: true,
      },
      annotation: {
        annotations,
      },
    },
    scales: {
      x: {
        grid: { display: false },
        border: { display: false },
      },
      y: {
        beginAtZero: true,
        grid: { display: true },
        ticks: {
          callback: (value) => Number(value).toFixed(1),
        },
      },
    },
  };

  return (
    <div className="col-span-12 rounded-sm border border-stroke bg-white px-5 pt-7.5 pb-5 shadow-default dark:border-strokedark dark:bg-boxdark sm:px-7.5 xl:col-span-12">
      <div className="flex flex-wrap items-start justify-between gap-3 sm:flex-nowrap">
        <div className="flex w-full flex-wrap gap-3 sm:gap-5">
          <div className="w-full">
            <p className="font-semibold ">{t("waterusage")}</p>
          </div>
        </div>
        <div className="flex w-full max-w-45 justify-end">
          <div className="inline-flex items-center rounded-md bg-whiter p-1.5 dark:bg-meta-4">
            <button
              className={isDay ? buttonActiveClasses : buttonDeactiveClasses}
              onClick={() => setActive("day")}
            >
              {t("day")}
            </button>
            <button
              className={isMonth ? buttonActiveClasses : buttonDeactiveClasses}
              onClick={() => setActive("month")}
            >
              {t("month")}
            </button>
            <button
              className={isYear ? buttonActiveClasses : buttonDeactiveClasses}
              onClick={() => setActive("year")}
            >
              {t("year")}
            </button>
          </div>
        </div>
      </div>

      <div>
        <div id="chartOne" className="-ml-5" style={{ height: 350 }}>
          <Bar data={chartData} options={options} />
        </div>
      </div>
    </div>
  );
};

export default ChartOne;
