import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * Meridian 프롬프트 캐시 및 세션 연속성 유지 익스텐션
 *
 * Pi가 Meridian(Claude Code SDK 기반 로컬 프록시)을 사용할 때,
 * 도구 실행 결과(tool_result)가 전달되는 턴에서 세션 식별자가 누락되어
 * Meridian이 독립 세션(isClientDrivenLoop)으로 판정하고 이전 전체 히스토리를
 * 매 턴마다 새 세션으로 다시 전송(fresh replay)하는 문제(Meridian 이슈 #734)를 방지합니다.
 *
 * 이 익스텐션은 요청 전송 직전(before_provider_request) 단계에서
 * Pi의 세션 식별자를 metadata.user_id 에 주입하여,
 * 도구 실행 턴에서도 90% 이상의 프롬프트 캐시 적중률(Cache Hit Rate)을 유지하도록 합니다.
 */
export default function (pi: ExtensionAPI) {
  let fallbackSessionId: string | null = null;

  pi.on("before_provider_request", (event: any, ctx: any) => {
    // 프로바이더가 meridian일 때만 동작하도록 제한합니다.
    if (ctx?.model?.provider !== "meridian") {
      return undefined;
    }

    const rawSessionId = ctx?.sessionManager?.getSessionId?.();
    let sessionId =
      typeof rawSessionId === "string" && rawSessionId ? rawSessionId : null;

    // 비영속 세션 모드 등에서 세션 ID를 가져오지 못하는 경우를 대비해 대체 식별자를 유지합니다.
    if (!sessionId) {
      if (!fallbackSessionId) {
        fallbackSessionId = `meerkit-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      }
      sessionId = fallbackSessionId;
    }

    const identity: Record<string, string> = { session_id: sessionId };

    // 서브에이전트 환경에서 부모 세션 식별자가 존재하는 경우 함께 포함합니다.
    const parentSessionId = ctx?.sessionManager?.getParentSessionId?.();
    if (typeof parentSessionId === "string" && parentSessionId) {
      identity.parent_session_id = parentSessionId;
    }

    const payload =
      event?.payload && typeof event.payload === "object" ? event.payload : {};
    const existingMetadata =
      payload.metadata && typeof payload.metadata === "object"
        ? payload.metadata
        : {};

    return {
      ...payload,
      metadata: {
        ...existingMetadata,
        user_id: JSON.stringify(identity),
      },
    };
  });
}
