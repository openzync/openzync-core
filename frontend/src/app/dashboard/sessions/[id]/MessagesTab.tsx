"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import {
  Box,
  Button,
  Card,
  Typography,
  ToggleButtonGroup,
  ToggleButton,
  CircularProgress,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
} from "@mui/material";
import TableChartIcon from "@mui/icons-material/TableChart";
import ChatIcon from "@mui/icons-material/Chat";
import { getSessionMessages, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type MessageResponse = components["schemas"]["MessageResponse"];

interface MessageRow {
  id: string;
  role: string;
  content: string;
  token_count: number;
  sequence_number: number;
  created_at: string;
  metadata: Record<string, unknown>;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatTime(raw: string): string {
  try {
    return new Date(raw).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function formatDate(raw: string): string {
  try {
    return new Date(raw).toLocaleString();
  } catch {
    return raw;
  }
}

// ─── Dark-theme chat bubble config ────────────────────────────────────────────

interface BubbleStyle {
  bg: string;
  borderColor: string;
  align: "flex-start" | "flex-end" | "center";
  label: string;
  textColor: string;
}

const BUBBLE_STYLES: Record<string, BubbleStyle> = {
  user: {
    bg: "#14488C",
    borderColor: "#14488C",
    align: "flex-end",
    label: "You",
    textColor: "#FFFFFF",
  },
  assistant: {
    bg: "#1A2332",
    borderColor: "#8FAFD9",
    align: "flex-start",
    label: "Assistant",
    textColor: "#E8EDF5",
  },
  system: {
    bg: "rgba(255, 255, 255, 0.04)",
    borderColor: "rgba(255, 255, 255, 0.08)",
    align: "center",
    label: "System",
    textColor: "text.secondary",
  },
  tool: {
    bg: "rgba(255, 255, 255, 0.04)",
    borderColor: "rgba(255, 255, 255, 0.08)",
    align: "center",
    label: "Tool",
    textColor: "text.secondary",
  },
};

function getBubbleStyle(role: string): BubbleStyle {
  return (
    BUBBLE_STYLES[role] ?? {
      bg: "rgba(255, 255, 255, 0.04)",
      borderColor: "rgba(255, 255, 255, 0.08)",
      align: "flex-start",
      label: role,
      textColor: "text.secondary",
    }
  );
}

// ─── Component Props ──────────────────────────────────────────────────────────

interface MessagesTabProps {
  userId: string;
  sessionId: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function MessagesTab({ userId, sessionId }: MessagesTabProps) {
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [viewMode, setViewMode] = useState<"chat" | "table">("chat");
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const chatEndRef = useRef<HTMLDivElement>(null);

  // Smooth scroll to bottom on first load
  useEffect(() => {
    if (!loading && messages.length > 0 && !cursor) {
      chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [loading, cursor, messages.length]);

  // ── Fetch messages ──────────────────────────────────────────────────────────

  const fetchMessages = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getSessionMessages(userId, sessionId, {
        limit: 100,
      });
      setMessages((result.data as MessageRow[]).reverse());
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      console.error("Failed to load messages", err);
    } finally {
      setLoading(false);
    }
  }, [userId, sessionId]);

  const loadOlder = useCallback(async () => {
    if (loadingMore || !cursor) return;
    setLoadingMore(true);
    try {
      const result = await getSessionMessages(userId, sessionId, {
        limit: 100,
        cursor,
      });
      const newMessages = (result.data as MessageRow[]).reverse();
      setMessages((prev) => [...newMessages, ...prev]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      console.error("Failed to load older messages", err);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, userId, sessionId]);

  useEffect(() => {
    fetchMessages();
  }, [fetchMessages]);

  // ── Render chat bubble ──────────────────────────────────────────────────────

  function renderChatBubble(msg: MessageRow) {
    const style = getBubbleStyle(msg.role);
    const isSystem = msg.role === "system" || msg.role === "tool";

    if (isSystem) {
      return (
        <Box
          key={msg.id}
          sx={{
            display: "flex",
            justifyContent: "center",
            mb: 1.5,
          }}
        >
          <Box
            sx={{
              maxWidth: "80%",
              bgcolor: style.bg,
              borderRadius: 1.5,
              px: 2,
              py: 1,
              border: "1px solid",
              borderColor: style.borderColor,
            }}
          >
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ fontWeight: 600, display: "block", mb: 0.5 }}
            >
              {style.label}
            </Typography>
            <Typography
              variant="body2"
              sx={{ fontFamily: "monospace", fontSize: "0.8rem", whiteSpace: "pre-wrap" }}
            >
              {msg.content}
            </Typography>
            <Typography
              variant="caption"
              color="text.disabled"
              sx={{ display: "block", mt: 0.5, textAlign: "right" }}
            >
              {formatTime(msg.created_at)}
              {msg.token_count > 0 && ` · ${msg.token_count} tokens`}
            </Typography>
          </Box>
        </Box>
      );
    }

    const isUser = msg.role === "user";

    return (
      <Box
        key={msg.id}
        sx={{
          display: "flex",
          justifyContent: style.align,
          mb: 1.5,
        }}
      >
        <Box
          sx={{
            maxWidth: "70%",
            bgcolor: style.bg,
            color: style.textColor,
            borderRadius: 2,
            borderBottomRightRadius: isUser ? 0 : 2,
            borderBottomLeftRadius: isUser ? 2 : 0,
            border: isUser ? "none" : "1px solid",
            borderColor: isUser ? "transparent" : style.borderColor,
            px: 2,
            py: 1.2,
          }}
        >
          <Typography
            variant="caption"
            sx={{
              fontWeight: 600,
              display: "block",
              mb: 0.5,
              opacity: 0.85,
              color: isUser ? "rgba(255,255,255,0.85)" : "rgba(232,237,245,0.7)",
            }}
          >
            {style.label}
          </Typography>
          <Typography
            variant="body2"
            sx={{
              whiteSpace: "pre-wrap",
              lineHeight: 1.5,
            }}
          >
            {msg.content}
          </Typography>
          <Typography
            variant="caption"
            sx={{
              display: "block",
              mt: 0.5,
              textAlign: isUser ? "right" : "left",
              opacity: 0.6,
              color: isUser ? "rgba(255,255,255,0.7)" : "text.disabled",
            }}
          >
            {formatTime(msg.created_at)}
            {msg.token_count > 0 && ` · ${msg.token_count} tokens`}
          </Typography>
        </Box>
      </Box>
    );
  }

  // ── Render table row (content with expand) ──────────────────────────────────

  function renderTableContent(row: MessageRow) {
    const isExpanded = expandedRows.has(row.id);
    const needsTruncation = row.content.length > 300;
    const text = isExpanded
      ? row.content
      : needsTruncation
        ? row.content.slice(0, 300) + "..."
        : row.content;

    return (
      <Box
        onClick={() => {
          if (needsTruncation) {
            setExpandedRows((prev) => {
              const next = new Set(prev);
              if (next.has(row.id)) next.delete(row.id);
              else next.add(row.id);
              return next;
            });
          }
        }}
        sx={{
          cursor: needsTruncation ? "pointer" : "default",
          whiteSpace: "pre-wrap",
          fontFamily: "monospace",
          fontSize: "0.8rem",
          lineHeight: 1.4,
          py: 0.5,
          maxHeight: isExpanded ? "none" : 80,
          overflow: "hidden",
        }}
      >
        {text}
      </Box>
    );
  }

  // ── Loading state ───────────────────────────────────────────────────────────

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (messages.length === 0) {
    return (
      <Card sx={{ p: 4, textAlign: "center" }}>
        <Typography variant="body1" color="text.secondary">
          No messages in this session yet.
        </Typography>
      </Card>
    );
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box>
      {/* View toggle */}
      <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 2 }}>
        <ToggleButtonGroup
          value={viewMode}
          exclusive
          onChange={(_e, val) => val && setViewMode(val)}
          size="small"
        >
          <ToggleButton value="chat">
            <ChatIcon fontSize="small" sx={{ mr: 0.5 }} />
            Chat
          </ToggleButton>
          <ToggleButton value="table">
            <TableChartIcon fontSize="small" sx={{ mr: 0.5 }} />
            Table
          </ToggleButton>
        </ToggleButtonGroup>
      </Box>

      {viewMode === "chat" ? (
        /* ── Chat bubble view ──────────────────────────────────────────────── */
        <Card sx={{ p: 3, maxHeight: "70vh", overflow: "auto" }}>
          {/* Load older button */}
          {hasMore && (
            <Box sx={{ display: "flex", justifyContent: "center", mb: 2 }}>
              <Button
                variant="outlined"
                size="small"
                onClick={loadOlder}
                disabled={loadingMore}
                startIcon={loadingMore ? <CircularProgress size={14} /> : undefined}
              >
                {loadingMore ? "Loading..." : "Load Older"}
              </Button>
            </Box>
          )}

          {messages.map(renderChatBubble)}
          <div ref={chatEndRef} />
        </Card>
      ) : (
        /* ── Table view ────────────────────────────────────────────────────── */
        <Card>
          <TableContainer>
            <Table size="small" sx={{ minWidth: 650 }}>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ width: 60, fontWeight: 600 }}>#</TableCell>
                  <TableCell sx={{ width: 100, fontWeight: 600 }}>Role</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Content</TableCell>
                  <TableCell sx={{ width: 80, fontWeight: 600, textAlign: "center" }}>
                    Tokens
                  </TableCell>
                  <TableCell sx={{ width: 160, fontWeight: 600 }}>Time</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {messages.map((msg) => (
                  <TableRow
                    key={msg.id}
                    sx={{
                      "&:hover": { bgcolor: "rgba(255,255,255,0.03)" },
                      "&:last-child td": { border: 0 },
                    }}
                  >
                    <TableCell sx={{ textAlign: "center", fontFamily: "monospace", fontSize: "0.8rem" }}>
                      {msg.sequence_number}
                    </TableCell>
                    <TableCell>
                      <Chip
                        label={msg.role}
                        size="small"
                        color={
                          msg.role === "user"
                            ? "primary"
                            : msg.role === "assistant"
                              ? "success"
                              : "default"
                        }
                        variant="outlined"
                      />
                    </TableCell>
                    <TableCell sx={{ maxWidth: 400 }}>
                      {renderTableContent(msg)}
                    </TableCell>
                    <TableCell sx={{ textAlign: "center", fontFamily: "monospace", fontSize: "0.8rem" }}>
                      {msg.token_count}
                    </TableCell>
                    <TableCell sx={{ fontSize: "0.8rem", whiteSpace: "nowrap" }}>
                      {formatDate(msg.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>

          {/* Load older button for table view */}
          {hasMore && (
            <Box sx={{ display: "flex", justifyContent: "center", p: 2 }}>
              <Button
                variant="outlined"
                onClick={loadOlder}
                disabled={loadingMore}
                startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}
              >
                {loadingMore ? "Loading..." : "Load Older"}
              </Button>
            </Box>
          )}
        </Card>
      )}
    </Box>
  );
}
