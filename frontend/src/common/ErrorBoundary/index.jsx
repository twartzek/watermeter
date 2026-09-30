import React from "react";

// Catches render errors that would otherwise crash the whole app to a white
// page (see git history/PR discussion: e.g. a failed API request during a
// short reboot of the Raspberry Pi made a component return an Error object
// instead of JSX -- React can't render that and throws; without this
// boundary it takes down the whole app).
//
// Only a class component can implement
// componentDidCatch/getDerivedStateFromError -- there is (as of now) no
// hook equivalent.
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
