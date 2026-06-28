"use client";

import { useState, useEffect } from "react";

interface FormattedDateProps {
  date: string;
  format?: Intl.DateTimeFormatOptions;
}

const defaultFormat: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "long",
  day: "numeric",
};

/**
 * Client component that renders a date in locale-aware format.
 * Server renders the raw ISO string to avoid hydration mismatch;
 * client hydrates with the formatted version on mount.
 */
export function FormattedDate({ date, format = defaultFormat }: FormattedDateProps) {
  const [formatted, setFormatted] = useState(date);

  useEffect(() => {
    try {
      setFormatted(
        new Date(date).toLocaleDateString("en-US", format),
      );
    } catch {
      // Fallback to raw date string on parse failure
    }
  }, [date, format]);

  return <>{formatted}</>;
}
