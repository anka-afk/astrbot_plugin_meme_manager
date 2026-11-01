import assert from "node:assert/strict";
import test from "node:test";
import { collectionConfidence, collectionReviewReason, collectionStages } from "../../pages/app/shared/collection-status.js";

test("missing and malformed probabilities remain unknown, including legacy records", () => {
  for (const value of [null, undefined, "", "0.9", NaN, Infinity, true, -0.1, 2]) {
    assert.equal(collectionConfidence(value), "未知");
  }
  assert.equal(collectionConfidence(0), "0%");
  assert.equal(collectionConfidence(0.97), "97%");
  assert.equal(collectionStages({}), "判断来源未知（旧记录）");
  assert.equal(collectionStages({judgment_backend: "vision"}), "视觉模型合并判断");
  assert.equal(collectionReviewReason({}), "");
});

test("decision failure and independent stages are visible without inventing a confidence", () => {
  const item = {judgment_backend: "decision", review_required: true,
    review_reasons: ["category_unknown", "decision_failed"],
    stage_status: {understanding: "completed", judgment: "failed"}};
  assert.match(collectionStages(item), /图片理解：完成.*决策：失败/);
  assert.match(collectionReviewReason(item), /无法确定现有分类.*决策判断失败/);
  assert.equal(collectionReviewReason({...item, review_reason: "<img src=x>"}), "<img src=x>");
});
