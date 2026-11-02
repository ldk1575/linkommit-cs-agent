# 근거 문서 출처 약칭

`docs/policy/*.md` 조항 끝의 `[확정: 약칭 …]` 과 `[비움]` 칸의 근거가 되는 링커밋 파일 목록입니다. 링커밋 저장소(비공개) 안의 경로이고, 이 저장소에는 복사하지 않았습니다. 확인일 2026-11-02.

| 약칭 | 링커밋 안의 경로 | 무엇을 확인했나 |
|---|---|---|
| `shop-purchase-flow-PRD` | `docs/features/shop-purchase-flow-PRD.md` | 구매 동선(U1~U19), 인수 기준(AC), 수량·재고·일부 취소, 비목표, 검산표 |
| `shop-order-history-PRD` | `docs/features/shop-order-history-PRD.md` | 주문내역·취소 규칙(U-1~U-7), 운송장 표시, 비목표, 구현 중 판단(§7-2) |
| `maker-claims-PRD` | `docs/features/maker-claims-PRD.md` | 소비자 취소 즉시 처리(D4), 판매자 사정 취소 사유 4종(C-3), 소비자 주문 상세 표시 |
| `consumer-shop-no-groupbuy-framing` | `docs/decisions/consumer-shop-no-groupbuy-framing.md` | 소비자 화면 원칙(판매 방식·수수료·정산 비노출, 할인율 유지) |
| `launch-checklist` | `docs/ops/launch-checklist.md` | 이용약관·정책이 법무 검토 전 초안(P1) |
| `ProductDetailView.tsx` | `apps/web/src/app/(shop)/products/[id]/ProductDetailView.tsx` | 상품 상세 탭, 출고 기한·무료배송 문구, 고시, 배송/교환/반품 문구 |
| `BuyBox.tsx` | `apps/web/src/app/(shop)/products/[id]/BuyBox.tsx` | 품절 표시, 남은 수량 표시 기준, 구매 불가 문구 |
| `checkout/page.tsx` | `apps/web/src/app/(shop)/products/[id]/checkout/page.tsx` | 주소 직접 접근 시 안내 문구 |
| `ShopFooter.tsx` | `apps/web/src/app/(shop)/_components/ShopFooter.tsx` | 통신판매중개자 고지 실문구, 사업자 정보 「확정 전」 |
| `shipping-policy.ts` | `apps/web/src/lib/shipping-policy.ts` | 출고 기한 1~14영업일 검증 |
| `order-history.ts` | `apps/web/src/lib/order-history.ts` | 배송 상태 배지 4종, 취소됨 우선 표시, 목록 카드 한 줄 |
| `OrderDetailView.tsx` | `apps/web/src/app/(shop)/orders/[id]/OrderDetailView.tsx` | 운송장 표시와 취소 불가 안내 |
| `support.ts` | `apps/web/src/lib/support.ts` | 고객센터 연락처가 아직 「확정 전」 |

참고한 레퍼런스(카테고리·장 구성 뼈대만, 문장은 새로 씀): 네이버 고객센터 대분류, 쿠팡 상품상세 「배송/교환/반품 안내」 탭.
