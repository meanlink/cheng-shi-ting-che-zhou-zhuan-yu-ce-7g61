-- 分流复盘工作台：接收场当天交班单与分流单车数认领。
-- 同一接收场、同一 UTC 运营日只有一份交班清点，多张分流单按单认领，
-- 已结案份额继续占用，复盘退回收回认领（同单号重试不重复扣减）。

CREATE TABLE IF NOT EXISTS handover_receipt (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lot_id bigint NOT NULL REFERENCES parking_lot(id),
    service_day date NOT NULL,
    reference text NOT NULL CHECK (length(trim(reference)) >= 4),
    total_vehicles integer NOT NULL CHECK (total_vehicles >= 0),
    created_by text NOT NULL REFERENCES staff(email),
    updated_by text REFERENCES staff(email),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reason text NOT NULL DEFAULT '接收场交班清点' CHECK (length(trim(reason)) >= 4),
    UNIQUE (lot_id, service_day)
);

CREATE TABLE IF NOT EXISTS diversion_claim (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    receipt_id bigint NOT NULL REFERENCES handover_receipt(id),
    diversion_id bigint NOT NULL REFERENCES parking_diversion(id),
    receipt_no text NOT NULL CHECK (length(trim(receipt_no)) >= 3),
    claimed_vehicles integer NOT NULL CHECK (claimed_vehicles > 0),
    status text NOT NULL DEFAULT 'held' CHECK (status IN ('held','released','settled')),
    created_by text NOT NULL REFERENCES staff(email),
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    release_note text,
    -- 同一认领回执号在一张交班单上只扣一次，重试直接返回原记录。
    UNIQUE (receipt_id, receipt_no),
    CHECK (status <> 'released' OR (released_at IS NOT NULL AND release_note IS NOT NULL)),
    CHECK (status = 'released' OR released_at IS NULL),
    CHECK (status = 'released' OR release_note IS NULL)
);
CREATE INDEX IF NOT EXISTS diversion_claim_receipt ON diversion_claim (receipt_id);
CREATE INDEX IF NOT EXISTS diversion_claim_diversion ON diversion_claim (diversion_id)
    WHERE status = 'held';
