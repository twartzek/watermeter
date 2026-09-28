import React from "react";
import useSWR from "swr";
import { DateTime } from "luxon";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import Loader from "../../common/Loader";

const fetcher = async (url) => fetch(url).then((res) => res.json());

// Entwickler-Karte: zeigt immer das zuletzt aufgenommene Foto an, auch wenn
// die Messwerterkennung fehlgeschlagen ist. Wird nur gerendert, wenn der
// Entwicklermodus in den Einstellungen aktiviert ist (siehe Settings.jsx).
function CardLastPhoto() {
  const { t } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  const url = "http://" + hostname + ":8000/api/v1/readings/last";
  const { data, error, isLoading } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  if (isLoading) return <Loader />;

  if (error || !data)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("apierror")}
      </div>
    );

  // id === null means the backend found no reading and no on-disk photo at
  // all (e.g. right after a fresh install/DB reset) -- that's simply "no
  // data yet", not an API error and not a failed detection.
  if (!data.imageUrl)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("nodata")}
      </div>
    );

  // measurementInProgress unterscheidet "noch kein Ergebnis, weil die
  // Auswertung noch laeuft" (Foto ist frisch, siehe restapi.py:
  // MEASUREMENT_IN_PROGRESS_THRESHOLD_SECONDS) von einem tatsaechlichen
  // Fehlschlag -- ohne das wuerde "Erkennung fehlgeschlagen" faelschlich
  // fuer die gesamte, mehrminuetige Auswertungsdauer angezeigt.
  // measurementCrashed unterscheidet zusaetzlich "Foto existiert, aber
  // kein DB-Reading, obwohl schon zu alt fuer 'laeuft noch'" (der Lauf ist
  // abgestuerzt oder wurde z.B. durch einen Watchdog-Reboot mitten in der
  // YOLO-Inferenz abgebrochen) von einem tatsaechlich ABGESCHLOSSENEN
  // Verwurf durch missingNeedleOrDigitDetector (dort existiert ein
  // DB-Reading mit discardReason in debugInfo).
  const measurementInProgress = data.measurementInProgress === true;
  const measurementCrashed = data.measurementCrashed === true;
  const detectionFailed =
    data.totalconsumption == null && !measurementInProgress && !measurementCrashed;
  // imageUrl wird bereits server-seitig auf die Bbox-annotierte Variante
  // aufgeloest, falls eine existiert (siehe restapi.py:_imageUrlFor) --
  // das ist auch bei einer verworfenen Messung der Fall (z.B. wegen einer
  // fehlenden Nadel-Box), damit der Entwicklermodus die erkannten Boxen
  // trotzdem sehen kann.
  const imageSrc = "http://" + hostname + ":8000" + data.imageUrl;
  const debugInfo = data.debugInfo;

  return (
    <div className="col-span-12 rounded-sm border border-stroke bg-white p-7.5 shadow-default dark:border-strokedark dark:bg-boxdark xl:col-span-4">
      <h4 className="mb-4 text-xl font-semibold text-black dark:text-white">
        {t("lastphoto")}
      </h4>
      <a href={imageSrc} target="_blank" rel="noreferrer">
        <img src={imageSrc} className="w-full rounded-md" />
      </a>
      <div className="mt-3 text-sm text-black dark:text-white">
        <span className="font-medium">{t("takenat")}: </span>
        {data.datetime
          ? DateTime.fromISO(data.datetime).toLocaleString(
              DateTime.DATETIME_MED
            )
          : "-"}
      </div>
      <div
        className={`text-sm ${
          detectionFailed || measurementCrashed
            ? "text-danger"
            : measurementInProgress
              ? "text-bodydark2"
              : "text-meta-5"
        }`}
      >
        <span className="font-medium text-black dark:text-white">
          {t("totalconsumption")}:{" "}
        </span>
        {measurementInProgress
          ? t("measurementinprogressshort")
          : measurementCrashed
            ? t("measurementcrashed")
            : detectionFailed
              ? t("detectionfailed")
              : data.totalconsumption.toFixed(4) + " m³"}
      </div>

      {debugInfo && (
        <div className="mt-3 flex flex-col gap-1.5 border-t border-stroke pt-3 text-sm dark:border-strokedark">
          {debugInfo.rawValue != null && (
            <div className="text-black dark:text-white">
              <span className="font-medium">{t("rawvalue")}: </span>
              {debugInfo.rawValue.toFixed(4)} m³
            </div>
          )}

          {(debugInfo.nNeedlesDetected != null ||
            debugInfo.nDigitsDetected != null) && (
            <div className="text-black dark:text-white">
              <span className="font-medium">
                {t("needlesdigitsdetected")}:{" "}
              </span>
              {debugInfo.nNeedlesDetected ?? "-"} /{" "}
              {debugInfo.nDigitsDetected ?? "-"}
            </div>
          )}

          {debugInfo.discardReason && (
            <div className="text-danger">
              <span className="font-medium">{t("discardreason")}: </span>
              {debugInfo.discardReason}
            </div>
          )}

          {debugInfo.filterSteps && debugInfo.filterSteps.length > 0 && (
            <div className="text-black dark:text-white">
              <span className="font-medium">{t("filterpipeline")}:</span>
              <ul className="mt-1 flex flex-col gap-1 pl-3">
                {debugInfo.filterSteps.map((step) => (
                  <li key={step.name}>
                    <span
                      className={
                        step.changed
                          ? "font-medium text-danger"
                          : "text-black dark:text-white"
                      }
                    >
                      {step.name}
                    </span>
                    : {step.before?.toFixed(4)} →{" "}
                    {step.after?.toFixed(4)} (
                    {step.changed ? t("filterchanged") : t("filterunchanged")}
                    )
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default CardLastPhoto;
