"use client";

import {
  createContext,
  useCallback,
  useContext,
  useState,
  useRef,
} from "react";
import { Snackbar, Alert, type AlertColor } from "@mui/material";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface Notification {
  id: number;
  message: string;
  severity: AlertColor;
}

interface NotificationContextValue {
  showNotification: (message: string, severity?: AlertColor) => void;
}

const NotificationContext = createContext<NotificationContextValue>({
  showNotification: () => {},
});

export const useNotification = () => useContext(NotificationContext);

// ─── Provider ──────────────────────────────────────────────────────────────────

export function NotificationProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const idRef = useRef(0);

  const showNotification = useCallback(
    (message: string, severity: AlertColor = "success") => {
      const id = ++idRef.current;
      setNotifications((prev) => [...prev, { id, message, severity }]);
    },
    [],
  );

  const handleClose = (id: number) => {
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  };

  return (
    <NotificationContext.Provider value={{ showNotification }}>
      {children}
      {notifications.map((notif, index) => (
        <Snackbar
          key={notif.id}
          open
          autoHideDuration={4000}
          onClose={() => handleClose(notif.id)}
          anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
          sx={{ mb: index * 7 }}
        >
          <Alert
            onClose={() => handleClose(notif.id)}
            severity={notif.severity}
            variant="filled"
            sx={{
              width: "100%",
              minWidth: 300,
              borderRadius: 1.5,
              boxShadow: "0 4px 20px rgba(0,0,0,0.3)",
            }}
          >
            {notif.message}
          </Alert>
        </Snackbar>
      ))}
    </NotificationContext.Provider>
  );
}
