import useSWR from "swr";
import { DateTime } from "luxon";
import { useTranslation } from "react-i18next";
import { BsFillTrash3Fill, BsChevronDown, BsChevronUp } from "react-icons/bs";
import React, { useMemo, useState, useRef } from "react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  TimeScale,
  PointElement,
  LineElement,
  Filler,
  Tooltip,
  Legend,
} from "chart.js";
import { Line } from "react-chartjs-2";
import zoomPlugin from "chartjs-plugin-zoom";
import annotationPlugin from "chartjs-plugin-annotation";
import "chartjs-adapter-luxon";
import Loader from "../../common/Loader";
import { enqueueSnackbar } from "notistack";
import ClickOutside from "../ClickOutside";

ChartJS.register(
  CategoryScale,
  LinearScale,
  TimeScale,
  PointElement,
  LineElement,
  Filler,
  Tooltip,
  Legend,
  zoomPlugin,
  annotationPlugin
);

// Roughly how tall 10 table rows are, so the reading list scrolls inside
// the card instead of growing the whole page.
const VISIBLE_ROWS = 10;
const ROW_HEIGHT_PX = 68;

// Every pipeline stage stored per reading (see db.Reading /
// restapi.py:PipelineStages), in pipeline order, that the developer-mode
// "Pipeline-Stufen" dropdown can plot as its own chart line. `key` indexes
// into a reading's pipelineStages object (only populated by the backend in
// developer mode); `color` intentionally avoids the two colors already
// used by the always-on totalconsumption/filteredTotal lines below.
const PIPELINE_STAGES = [
  { key: "rawYolo", labelKey: "stagerawyolo", color: "#FDB022" },
  { key: "afterMissingDigit", labelKey: "stageaftermissingdigit", color: "#0FADCF" },
  { key: "afterMaxFlow", labelKey: "stageaftermaxflow", color: "#E67E22" },
  { key: "afterNegativeDelta", labelKey: "stageafternegativedelta", color: "#2ECC71" },
];

const fetcher = async (url) => fetch(url).then((res) => res.json());

const TableOne = () => {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);
  const [selectedReading, setSelectedReading] = useState(null);
  const [popoverPosition, setPopoverPosition] = useState({ x: 0, y: 0 });
  const [isTableExpanded, setIsTableExpanded] = useState(false);
  const [selectedStages, setSelectedStages] = useState([]);
  const [isStageMenuOpen, setIsStageMenuOpen] = useState(false);
  const chartRef = useRef(null);

  const startDateString = DateTime.local({ zone: "utc" })
    .endOf("day")
    .minus({ days: 2 })
    .toISO();
  const endDateString = DateTime.local({ zone: "utc" }).endOf("day").toISO();

  const url =
    "http://" +
    hostname +
    ":8000/api/v1/readings/?start=" +
    startDateString +
    "&end=" +
    endDateString;

  const { data, error, isLoading, mutate } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  const notificationsUrl = "http://" + hostname + ":8000/api/v1/notifications";
  const { data: notifications } = useSWR(notificationsUrl, fetcher, {
    refreshInterval: 60000,
  });

  const settingsUrl = "http://" + hostname + ":8000/api/v1/settings";
  const { data: settings } = useSWR(settingsUrl, fetcher, {});
  const developerMode = settings?.developerMode ?? false;

  function toggleStage(key) {
    setSelectedStages((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
    );
  }

  async function deleteReading(id) {
    try {
      const response = await fetch(
        "http://" + hostname + ":8000/api/v1/readings/" + id,
        {
          method: "DELETE",
        }
      );
      if (!response.ok) {
        enqueueSnackbar(t("deletereadingerror"), { variant: "error" });
      } else {
        enqueueSnackbar(t("deletereadingsuccess"), { variant: "success" });
        mutate();
      }
    } catch (error) {
      enqueueSnackbar(t("deletereadingerror") + " " + error, {
        variant: "error",
      });
    }
  }

  // Oldest-first order matches the chart's x-axis; keep it around so a
  // point click index can be mapped back to the full reading object (image,
  // exact timestamp) for the popover.
  const reversedData = useMemo(() => (data ? data.toReversed() : []), [data]);

  const chartData = useMemo(() => {
    const baseDatasets = [
      {
        label: t("totalconsumption"),
        data: reversedData.map((item) => ({
          x: DateTime.fromISO(item.datetime).toJSDate(),
          y: item.totalconsumption,
        })),
        borderColor: "#80CAEE",
        backgroundColor: "#80CAEE33",
        pointBackgroundColor: "#fff",
        pointBorderColor: "#80CAEE",
        pointBorderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 7,
        borderWidth: 2,
        fill: true,
        tension: 0,
      },
      {
        label: t("filteredtotal"),
        data: reversedData.map((item) => ({
          x: DateTime.fromISO(item.datetime).toJSDate(),
          y: item.filteredTotal,
        })),
        borderColor: "#3C50E0",
        backgroundColor: "#3C50E033",
        pointBackgroundColor: "#fff",
        pointBorderColor: "#3C50E0",
        pointBorderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 7,
        borderWidth: 2,
        fill: true,
        tension: 0,
      },
    ];

    // Developer mode only, and only for stages the user actually picked
    // (see PIPELINE_STAGES) -- each becomes its own unfilled, dashed line
    // so it reads as a diagnostic overlay rather than another "real"
    // consumption series alongside totalconsumption/filteredTotal above.
    const stageDatasets = developerMode
      ? PIPELINE_STAGES.filter((stage) => selectedStages.includes(stage.key)).map(
          (stage) => ({
            label: t(stage.labelKey),
            data: reversedData.map((item) => ({
              x: DateTime.fromISO(item.datetime).toJSDate(),
              y: item.pipelineStages ? item.pipelineStages[stage.key] : null,
            })),
            borderColor: stage.color,
            backgroundColor: stage.color + "33",
            pointBackgroundColor: "#fff",
            pointBorderColor: stage.color,
            pointBorderWidth: 2,
            pointRadius: 2,
            pointHoverRadius: 6,
            borderWidth: 1.5,
            borderDash: [5, 3],
            fill: false,
            tension: 0,
            spanGaps: true,
          })
        )
      : [];

    return { datasets: [...baseDatasets, ...stageDatasets] };
  }, [reversedData, t, developerMode, selectedStages]);

  // Mark points in time where the leakage detector or the sustained-high-
  // flow (possible pipe burst) check fired a warning, so a spike/rise in
  // the chart can be visually correlated with the alert.
  const leakageAnnotations = useMemo(() => {
    const leakageNotifications = Array.isArray(notifications)
      ? notifications.filter(
          (item) =>
            (item.i18nIdentifier === "leakdetected" ||
              item.i18nIdentifier === "highflowdetected") &&
            item.type === "warning"
        )
      : [];

    return Object.fromEntries(
      leakageNotifications.map((item, index) => [
        `leakage-${index}`,
        {
          type: "line",
          xMin: DateTime.fromISO(item.time).toJSDate().getTime(),
          xMax: DateTime.fromISO(item.time).toJSDate().getTime(),
          borderColor: "#FB5454",
          borderWidth: 1,
          borderDash: [4, 4],
          label: {
            display: true,
            content:
              item.i18nIdentifier === "highflowdetected"
                ? t("highflowlabel")
                : t("leakagedetected"),
            position: "start",
            backgroundColor: "#FB5454",
            color: "#fff",
            font: { size: 10 },
          },
        },
      ])
    );
  }, [notifications, t]);

  // Passing a fresh options object on every render is fine for Chart.js
  // (unlike the previous ApexCharts setup, it does not tear down and
  // reinitialize the chart instance), but memoizing still avoids rebuilding
  // scales/plugins on every unrelated re-render.
  const options = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "nearest",
        intersect: true,
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
          align: "start",
        },
        tooltip: {
          enabled: true,
          callbacks: {
            title: (items) =>
              items.length
                ? DateTime.fromMillis(items[0].parsed.x).toFormat(
                    "dd MMM yyyy, HH:mm"
                  )
                : "",
          },
        },
        zoom: {
          pan: {
            enabled: true,
            mode: "xy",
          },
          zoom: {
            wheel: { enabled: true },
            pinch: { enabled: true },
            drag: { enabled: false },
            mode: "xy",
          },
        },
        annotation: {
          annotations: leakageAnnotations,
        },
      },
      scales: {
        x: {
          type: "time",
          time: {
            tooltipFormat: "dd MMM yyyy, HH:mm",
            displayFormats: {
              hour: "dd MMM, HH:mm",
              minute: "dd MMM, HH:mm",
            },
          },
          grid: { display: false },
          border: { display: false },
        },
        y: {
          grid: { display: true },
          ticks: {
            callback: (value) => Number(value).toFixed(3),
          },
        },
      },
      onClick: (event, elements) => {
        if (!elements.length) return;
        const reading = reversedData[elements[0].index];
        if (!reading) return;
        setSelectedReading(reading);
        const native = event.native ?? event;
        setPopoverPosition({
          x: native?.clientX ?? native?.pageX ?? 0,
          y: native?.clientY ?? native?.pageY ?? 0,
        });
      },
    }),
    [reversedData, leakageAnnotations]
  );

  if (isLoading) return <Loader />;

  if (error) return <div>failed to load</div>;

  return (
    <div className="rounded-sm border border-stroke bg-white px-5 pt-6 pb-2.5 shadow-default dark:border-strokedark dark:bg-boxdark sm:px-7.5 xl:pb-1">
      <div className="mb-6 flex items-center justify-between">
        <h4 className="text-xl font-semibold text-black dark:text-white">
          {t("lastreadings")}
        </h4>
        <div className="flex items-center gap-2">
          {developerMode && (
            <ClickOutside
              onClick={() => setIsStageMenuOpen(false)}
              className="relative"
            >
              <button
                type="button"
                className="flex items-center gap-1.5 rounded py-1 px-3 text-xs font-medium text-black hover:bg-white hover:shadow-card dark:text-white dark:hover:bg-boxdark"
                onClick={() => setIsStageMenuOpen((prev) => !prev)}
                aria-expanded={isStageMenuOpen}
              >
                {t("pipelinestages")}
                {selectedStages.length > 0 && (
                  <span className="rounded-full bg-primary px-1.5 py-0.5 text-[10px] font-semibold text-white">
                    {selectedStages.length}
                  </span>
                )}
                {isStageMenuOpen ? <BsChevronUp /> : <BsChevronDown />}
              </button>
              {isStageMenuOpen && (
                <div className="absolute right-0 top-full z-20 mt-1 w-64 rounded-sm border border-stroke bg-white p-2 shadow-default dark:border-strokedark dark:bg-boxdark">
                  {PIPELINE_STAGES.map((stage) => (
                    <label
                      key={stage.key}
                      className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-black hover:bg-gray dark:text-white dark:hover:bg-meta-4"
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 rounded border-stroke"
                        checked={selectedStages.includes(stage.key)}
                        onChange={() => toggleStage(stage.key)}
                      />
                      <span
                        className="h-2.5 w-2.5 flex-shrink-0 rounded-full"
                        style={{ backgroundColor: stage.color }}
                      />
                      {t(stage.labelKey)}
                    </label>
                  ))}
                </div>
              )}
            </ClickOutside>
          )}
          <button
            className="rounded py-1 px-3 text-xs font-medium text-black hover:bg-white hover:shadow-card dark:text-white dark:hover:bg-boxdark"
            onClick={() => chartRef.current?.resetZoom()}
          >
            {t("resetzoom")}
          </button>
        </div>
      </div>
      <div style={{ height: 300 }}>
        <Line ref={chartRef} data={chartData} options={options} />
      </div>

      <button
        type="button"
        className="mt-2 flex w-full items-center justify-between border-t border-stroke py-3 text-sm font-medium text-black dark:border-strokedark dark:text-white"
        onClick={() => setIsTableExpanded((prev) => !prev)}
        aria-expanded={isTableExpanded}
      >
        {isTableExpanded ? t("hidetable") : t("showtable")}
        {isTableExpanded ? <BsChevronUp /> : <BsChevronDown />}
      </button>

      {isTableExpanded && (
        <div
          className="overflow-x-auto overflow-y-auto"
          style={{ maxHeight: VISIBLE_ROWS * ROW_HEIGHT_PX }}
        >
          <table className="w-full border-collapse">
            <thead className="sticky top-0 z-10">
              <tr className="bg-gray-2 dark:bg-meta-4 text-left">
                <th className="p-3 text-sm font-medium uppercase">
                  {t("image")}
                </th>
                <th className="p-3 text-sm font-medium uppercase text-center">
                  {t("takenat")}
                </th>
                <th className="p-3 text-sm font-medium uppercase text-center">
                  {t("totalconsumption")}
                </th>
                <th className="p-3 text-sm font-medium uppercase text-center">
                  {t("filteredtotal")}
                </th>
                <th className="p-3 text-sm font-medium uppercase text-center"></th>
              </tr>
            </thead>
            <tbody>
              {data.map((reading, index) => {
                // Bei fehlgeschlagener Erkennung (nur im Entwicklermodus
                // sichtbar) wird keine _bbox.jpg erzeugt, daher faellt das
                // Bild auf das unbearbeitete Originalfoto zurueck.
                const detectionFailed = reading.totalconsumption == null;
                const imageSrc =
                  "http://" +
                  hostname +
                  ":8000" +
                  reading.imageUrl +
                  (detectionFailed ? "" : "_bbox.jpg");

                return (
                  <tr
                    key={index}
                    className={`border-b dark:border-strokedark ${
                      index === data.length - 1 ? "" : "border-stroke"
                    }`}
                  >
                    <td className="p-3 flex items-center gap-3">
                      <a href={imageSrc} target="_blank">
                        <img
                          src={imageSrc}
                          // alt={reading.imageUrl}
                          className="w-12 h-12 rounded-md"
                        />
                      </a>
                    </td>
                    <td className="p-3 text-center text-black dark:text-white">
                      {DateTime.fromISO(reading.datetime).toLocaleString(
                        DateTime.DATETIME_MED
                      )}
                    </td>
                    <td className="p-3 text-center text-meta-5">
                      {detectionFailed
                        ? t("detectionfailed")
                        : reading.totalconsumption.toFixed(4) + " m³"}
                    </td>
                    <td className="p-3 text-center text-black dark:text-white">
                      {reading.filteredTotal == null
                        ? "-"
                        : reading.filteredTotal.toFixed(4) + " m³"}
                    </td>
                    <td className="border-b border-[#eee] py-5 px-4 dark:border-strokedark">
                      <div className="flex items-center space-x-3.5">
                        <button
                          className="hover:text-primary"
                          onClick={() => deleteReading(reading.id)}
                        >
                          <BsFillTrash3Fill />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {selectedReading && (
        <ClickOutside
          onClick={() => setSelectedReading(null)}
          className="fixed z-999 w-64 rounded-sm border border-stroke bg-white p-4 shadow-default dark:border-strokedark dark:bg-boxdark"
          style={{
            left: Math.min(popoverPosition.x, window.innerWidth - 272),
            top: popoverPosition.y + 12,
          }}
        >
          <img
            src={
              "http://" +
              hostname +
              ":8000" +
              selectedReading.imageUrl +
              (selectedReading.totalconsumption == null ? "" : "_bbox.jpg")
            }
            className="mb-3 w-full rounded-md"
          />
          <div className="mb-1 text-sm text-black dark:text-white">
            <span className="font-medium">{t("takenat")}: </span>
            {DateTime.fromISO(selectedReading.datetime).toLocaleString(
              DateTime.DATETIME_MED
            )}
          </div>
          <div className="text-sm text-meta-5">
            <span className="font-medium text-black dark:text-white">
              {t("totalconsumption")}:{" "}
            </span>
            {selectedReading.totalconsumption == null
              ? t("detectionfailed")
              : selectedReading.totalconsumption.toFixed(4) + " m³"}
          </div>
          <div className="text-sm text-black dark:text-white">
            <span className="font-medium">{t("filteredtotal")}: </span>
            {selectedReading.filteredTotal == null
              ? "-"
              : selectedReading.filteredTotal.toFixed(4) + " m³"}
          </div>
        </ClickOutside>
      )}
    </div>
  );
};

export default TableOne;
