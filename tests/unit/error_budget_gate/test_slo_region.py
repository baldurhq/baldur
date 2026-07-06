"""
SLO 리전 필드 테스트.

SLO dataclass의 region 필드 존재 및 기본값 검증.
"""

from baldur.slo import SLI, SLO

# =============================================================================
# 계약 검증: SLO region 필드
# =============================================================================


class TestSloRegionFieldContract:
    """SLO.region 필드 계약 검증."""

    def test_region_field_exists(self):
        """SLO에 region 필드 존재."""
        slo = SLO(name="test", sli=SLI.AVAILABILITY, target=0.999)
        assert hasattr(slo, "region")

    def test_region_default_none(self):
        """SLO.region 기본값 None."""
        slo = SLO(name="test", sli=SLI.AVAILABILITY, target=0.999)
        assert slo.region is None


# =============================================================================
# 동작 검증: SLO region 설정
# =============================================================================


class TestSloRegionBehavior:
    """SLO.region 동작 검증."""

    def test_region_settable(self):
        """SLO 생성 시 region 지정 가능."""
        slo = SLO(
            name="availability_seoul",
            sli=SLI.AVAILABILITY,
            target=0.999,
            region="seoul",
        )
        assert slo.region == "seoul"

    def test_region_and_domain_coexist(self):
        """region과 기존 domain 필드 독립적 존재."""
        slo = SLO(
            name="test",
            sli=SLI.AVAILABILITY,
            target=0.999,
            domain="payment",
            region="seoul",
        )
        assert slo.domain == "payment"
        assert slo.region == "seoul"
