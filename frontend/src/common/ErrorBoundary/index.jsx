import React from "react";

// Faengt Render-Fehler ab, die sonst die komplette App zu einer weissen
// Seite abstuerzen lassen wuerden (siehe git history/PR-Diskussion: ein
// fehlgeschlagener API-Request waehrend eines kurzen Reboots des
// Raspberry Pi fuehrte z.B. dazu, dass eine Komponente ein Error-Objekt
// statt JSX zurueckgab -- React kann das nicht rendern und wirft, ohne
// diese Boundary reisst das die gesamte App runter).
//
// Nur eine Klassenkomponente kann componentDidCatch/getDerivedStateFromError
// implementieren -- es gibt dafuer (Stand jetzt) kein Hook-Aequivalent.
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error, info) {
    console.error("ErrorBoundary caught an error:", error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-screen flex-col items-center justify-center gap-4 bg-white text-center dark:bg-boxdark dark:text-bodydark">
          <p className="text-lg font-medium">
            Etwas ist schiefgelaufen. Die Seite wird in Kürze automatisch
            wieder funktionieren, sobald der Server wieder erreichbar ist.
          </p>
          <button
            className="rounded bg-primary px-4 py-2 text-white"
            onClick={() => window.location.reload()}
          >
            Seite neu laden
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
