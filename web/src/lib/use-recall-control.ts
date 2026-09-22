"use client";
import { useEffect, useRef } from "react";
import { startRecallControl, type RecallControlOptions } from "./recall-control";

export function useRecallControl(options: RecallControlOptions) {
  const latest = useRef(options);
  latest.current = options;
  useEffect(() => {
    if (options.enabled) return startRecallControl(() => latest.current);
  }, [options.enabled]);
}
