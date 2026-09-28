import React from "react";
import CardDataStats from "../../components/CardDataStats";
import useSWR from "swr";
import { BsSpeedometer } from "react-icons/bs";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import { DateTime } from "luxon";
import CardDataStatsLoading from "../../components/CardDataStatsLoading";

const fetcher = async (url) => fetch(url).then((res) => res.json());

function CardDataStatsCum() {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  // /readings/lastsuccessful statt /readings/last: liefert immer den
  // letzten *erfolgreichen* Wert (unabhaengig vom Entwicklermodus) -- ein
  // einzelner fehlgeschlagener Messversuch (z.B. ein Watchdog-Reboot mitten
  // in der Inferenz) soll hier nicht "Keine Daten" zeigen, solange zuvor
  // ein gueltiger Wert vorlag. isStale markiert, wenn dieser letzte
  // erfolgreiche Wert bereits laenger zurueckliegt, damit ein tagealter
  // Wert nicht unbemerkt als aktuell durchgeht (siehe restapi.py).
  const url = "http://" + hostname + ":8000/api/v1/readings/lastsuccessful";

  const { data, error, isLoading } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  if (isLoading) return <CardDataStatsLoading />;

  if (error)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("measurementinprogress")}
      </div>
    );

  // Diese Kachel zeigt den per OCR erkannten aktuellen Zaehlerstand -- den
  // rohen Wert (data.totalconsumption), so wie er auf dem Zaehler abzulesen
  // ist. Der ueber Zaehlertausche hinweg fortgeschriebene Gesamtverbrauch
  // (data.cumulativeTotal) hat eine eigene Kachel, siehe
  // CardDataStatsCumulative.jsx.
  //
  // id === null bedeutet: es gibt noch gar keine erfolgreiche Reading
  // (frische Installation / DB-Reset) -- das ist "keine Daten".
  const noDataYet = data.id == null;
  return (
    <CardDataStats
      title={t("cumtoday")}
      total={noDataYet ? t("nodata") : data.totalconsumption.toFixed(2) + " m³"}
      subtitle={
        !noDataYet &&
        t(data.isStale ? "stalereading" : "asof", {
          datetime: DateTime.fromISO(data.datetime).toLocaleString(
            DateTime.DATETIME_MED
          ),
        })
      }
      stale={!noDataYet && data.isStale}
    >
      <BsSpeedometer
        className="fill-primary dark:fill-white"
        style={{ fontSize: "1.5em" }}
      />
    </CardDataStats>
  );
}

export default CardDataStatsCum;
